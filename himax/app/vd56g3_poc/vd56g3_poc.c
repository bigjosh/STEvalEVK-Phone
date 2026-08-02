/*
 * vd56g3_poc.c — one-shot VD56G3 capture on Grove Vision AI V2 (WE2)
 *
 * Boot -> init sensor (proven EVK sequence) -> arm INP->WDMA2 raw path ->
 * START_STREAM -> first frame lands in SRAM -> stop -> CRC32 -> base64 dump
 * over the console UART (921600) framed by BEGIN/END markers -> halt.
 *
 * The frame is packed RAW10 carried as DT RAW8: 1400 bytes x 1360 rows.
 * PC side: himax/receive_frame.py in the STEvalEVK-Phone repo.
 * Reset the board (DTR toggle or button) for another capture.
 */

#include <stdio.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "WE2_device.h"
#include "board.h"
#include "xprintf.h"
#include "WE2_core.h"
#include "hx_drv_scu.h"
#include "hx_drv_swreg_aon.h"
#include "hx_drv_timer.h"

#include "hx_drv_csirx.h"

#include "cisdp_sensor.h"
#include "vd56g3_poc.h"

#define APP_BUILD_TAG "vd56g3_poc build 2026-08-02a"

static volatile uint32_t g_frame_ready = 0;
static volatile uint32_t g_cur_frame = 0;
static volatile uint32_t g_dp_event_err = 0;

static void app_halt(const char *why)
{
	xprintf("HALT: %s\r\n", why);
	while (1) {
		hx_drv_timer_cm55x_delay_ms(1000, TIMER_STATE_DC);
	}
}

static void app_dplib_cb(SENSORDPLIB_STATUS_E event)
{
	switch (event) {
	case SENSORDPLIB_STATUS_XDMA_FRAME_READY:
		g_cur_frame++;
		g_frame_ready = 1;
		break;
	case SENSORDPLIB_STATUS_XDMA_WDMA2_FINISH:
		break;
	default:
		/* Any abnormal/timeout event: log the raw code; bring-up gold. */
		xprintf("dplib event: %d\r\n", event);
		g_dp_event_err = event;
		break;
	}
}

static uint32_t crc32_calc(const uint8_t *p, uint32_t n)
{
	uint32_t crc = 0xFFFFFFFFu;
	for (uint32_t i = 0; i < n; i++) {
		crc ^= p[i];
		for (int b = 0; b < 8; b++)
			crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
	}
	return crc ^ 0xFFFFFFFFu;
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void dump_base64(const uint8_t *p, uint32_t n)
{
	char line[97];
	uint32_t li = 0;
	uint32_t i = 0;

	while (i < n) {
		uint32_t rem = n - i;
		uint32_t v;
		if (rem >= 3) {
			v = ((uint32_t)p[i] << 16) | ((uint32_t)p[i+1] << 8) | p[i+2];
			line[li++] = B64[(v >> 18) & 63];
			line[li++] = B64[(v >> 12) & 63];
			line[li++] = B64[(v >> 6) & 63];
			line[li++] = B64[v & 63];
			i += 3;
		} else if (rem == 2) {
			v = ((uint32_t)p[i] << 16) | ((uint32_t)p[i+1] << 8);
			line[li++] = B64[(v >> 18) & 63];
			line[li++] = B64[(v >> 12) & 63];
			line[li++] = B64[(v >> 6) & 63];
			line[li++] = '=';
			i += 2;
		} else {
			v = ((uint32_t)p[i] << 16);
			line[li++] = B64[(v >> 18) & 63];
			line[li++] = B64[(v >> 12) & 63];
			line[li++] = '=';
			line[li++] = '=';
			i += 1;
		}
		if (li >= 96) {
			line[li] = 0;
			xprintf("%s\n", line);
			li = 0;
		}
	}
	if (li) {
		line[li] = 0;
		xprintf("%s\n", line);
	}
}

/* Framework's app/main.c only provides main() for the stock app defines;
 * for VD56G3_POC it compiles empty, so the entry lives here. */
int main(void)
{
	board_init();
	app_main();
	return 0;
}

int app_main(void)
{
	xprintf("\r\n=== %s ===\r\n", APP_BUILD_TAG);

	if (cisdp_sensor_init() < 0)
		app_halt("cisdp_sensor_init failed");

	/*
	 * Link bring-up matrix: the D-PHY clock lane is known-good (continuous HS
	 * seen) but no packets decode at the baseline config. Sweep sensor
	 * bitrate x lane-polarity x RX settle count until a frame lands.
	 */
	/*
	 * OIF_CTRL topologies (power-on default reads 0x0240 = both lane
	 * polarities swapped, lane-count field 0):
	 *   bits2:0 lane count, bit3 clk swap, bits5:4 lane0 phys mapping,
	 *   bit6 lane0 pol swap, bits8:7 lane1 phys mapping, bit9 lane1 pol swap.
	 * The 1-lane rows steer the single logical lane onto each physical pair
	 * in both polarities — if none of those sync, the data pairs are not
	 * physically reaching the WE2.
	 */
	static const struct { uint16_t mbps; uint16_t oif; uint8_t lanes; uint8_t hscnt; } tries[] = {
		{ 804, 0x0242, 2, 0x10 },	/* default polarity, explicit 2-lane */
		{ 804, 0x0240, 2, 0x10 },	/* untouched power-on default */
		{ 804, 0x0002, 2, 0x10 },	/* no swaps */
		{ 804, 0x02D2, 2, 0x10 },	/* mappings crossed, swaps kept (m0=1,m1=0: 0x242^0x90^0x180) */
		{ 804, 0x0041, 1, 0x10 },	/* 1-lane, pair A, default polarity */
		{ 804, 0x0001, 1, 0x10 },	/* 1-lane, pair A, no swap */
		{ 804, 0x0051, 1, 0x10 },	/* 1-lane, pair B, swap */
		{ 804, 0x0011, 1, 0x10 },	/* 1-lane, pair B, no swap */
		{ 804, 0x0248, 2, 0x10 },	/* clk lane polarity swapped too */
		{ 500, 0x0242, 2, 0x08 },	/* slow fallback */
	};

	int hit = -1;
	for (unsigned t = 0; t < sizeof(tries)/sizeof(tries[0]); t++) {
		xprintf("--- TRY %d: bitrate=%d oif=0x%04x lanes=%d hscnt=0x%02x ---\r\n",
				t, tries[t].mbps, tries[t].oif, tries[t].lanes, tries[t].hscnt);

		if (cisdp_set_link(tries[t].mbps, tries[t].oif) < 0) {
			xprintf("  set_link failed (sensor unhappy) — skipping\r\n");
			continue;
		}

		/* clean datapath re-arm */
		sensordplib_stop_capture();
		sensordplib_start_swreset();
		sensordplib_stop_swreset_WoSensorCtrl();

		cisdp_csirx_reinit(tries[t].mbps, tries[t].hscnt, tries[t].lanes);

		if (cisdp_dp_init(true, CISDP_INIT_TYPE_FULL_RAW, SENSORDPLIB_PATH_INP_WDMA2, app_dplib_cb) < 0)
			app_halt("cisdp_dp_init failed");

		g_frame_ready = 0;
		g_dp_event_err = 0;

		/* Sensor is in SW_STANDBY here: lanes must be LP-11 (stop=1) if they
		 * are physically connected. stop=0 with no possible HS clock means
		 * the pin is floating at the WE2 PHY. */
		xprintf("  pre-start stop(clk,l0,l1)=%d,%d,%d (expect 1,1,1 if wired)\r\n",
				hx_drv_csirx_get_clkln_stopstate(),
				hx_drv_csirx_get_ln0_stopstate(),
				hx_drv_csirx_get_ln1_stopstate());

		if (cisdp_sensor_start() < 0) {
			xprintf("  sensor start failed\r\n");
			continue;
		}

		uint32_t waited = 0;
		while (g_frame_ready == 0 && waited < 1500) {
			hx_drv_timer_cm55x_delay_ms(1, TIMER_STATE_DC);
			waited++;
		}

		uint32_t info = 0, err = 0, phyerr = 0;
		hx_drv_csirx_get_infoirq_state(&info);
		hx_drv_csirx_get_errirq_state(&err);
		hx_drv_csirx_get_dphyerrirq_state(&phyerr);
		xprintf("  -> frame=%d dp_evt=%d info=0x%08x err=0x%08x phyerr=0x%08x stop=%d,%d,%d\r\n",
				g_frame_ready, g_dp_event_err, info, err, phyerr,
				hx_drv_csirx_get_clkln_stopstate(),
				hx_drv_csirx_get_ln0_stopstate(),
				hx_drv_csirx_get_ln1_stopstate());

		if (g_frame_ready) {
			hit = t;
			xprintf("*** LINK UP with bitrate=%d oif=0x%04x lanes=%d hscnt=0x%02x ***\r\n",
					tries[t].mbps, tries[t].oif, tries[t].lanes, tries[t].hscnt);
			break;
		}
	}

	if (hit < 0) {
		uint8_t fsm = 0; uint16_t serr = 0;
		vd56g3_read_u8(0x0028, &fsm);
		vd56g3_read_u16(0x001C, &serr);
		xprintf("matrix exhausted: FSM=%d ERROR_CODE=0x%04x\r\n", fsm, serr);
		app_halt("no link config worked");
	}

	xprintf("frame %d captured\r\n", g_cur_frame);

	/* One frame is enough — quiesce everything before the slow dump. */
	cisdp_sensor_stop();

	uint32_t addr = app_get_raw_addr();
	uint32_t size = app_get_raw_sz();

	hx_InvalidateDCache_by_Addr((volatile void *)addr, size);

	uint32_t crc = crc32_calc((const uint8_t *)addr, size);

	/* Applied-exposure readback for the record (STATUS regs, SENSOR.md sec.8) */
	uint16_t coarse = 0;
	vd56g3_read_u16(0x0064, &coarse);

	xprintf("\r\n===VD56G3_FRAME_BEGIN len=%d w=%d h=%d bpp=8 crc32=%08x coarse=%d===\n",
			size, app_get_raw_width(), app_get_raw_height(), crc, coarse);
	dump_base64((const uint8_t *)addr, size);
	xprintf("===VD56G3_FRAME_END crc32=%08x===\n", crc);

	xprintf("done — reset board for another capture\r\n");
	while (1) {
		hx_drv_timer_cm55x_delay_ms(1000, TIMER_STATE_DC);
	}
	return 0;
}
