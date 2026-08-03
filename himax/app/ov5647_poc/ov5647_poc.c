/*
 * ov5647_poc.c — one-shot OV5647 raw capture through the SAME path the
 * VD56G3 POC uses (INP -> WDMA2 raw dump -> CRC -> base64 over console UART).
 *
 * Purpose: (a) prove the raw-capture + serial-dump + PC-receiver pipeline
 * end-to-end with a known-good camera, (b) print the PHY/IRQ/register
 * signature of a WORKING MIPI link for comparison against the VD56G3 chain.
 *
 * Frame: 1280x960, 8 bits/pixel (INP stores 1 B/px), Bayer mosaic (color
 * sensor) — expect a checkerboard texture in the grayscale render.
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
#include "ov5647_poc.h"

#define APP_BUILD_TAG "ov5647_poc build 2026-08-02a"

static volatile uint32_t g_frame_ready = 0;
static volatile uint32_t g_cur_frame = 0;
static volatile int32_t g_dp_event_err = 0;

static void app_halt(const char *why)
{
	xprintf("HALT: %s\r\n", why);
	while (1) {
		hx_drv_timer_cm55x_delay_ms(1000, TIMER_STATE_DC);
	}
}

static void csirx_signature(const char *tag)
{
	uint32_t info = 0, err = 0, phyerr = 0;
	hx_drv_csirx_get_infoirq_state(&info);
	hx_drv_csirx_get_errirq_state(&err);
	hx_drv_csirx_get_dphyerrirq_state(&phyerr);
	xprintf("[SIG %s] info=0x%08x err=0x%08x phyerr=0x%08x stop(clk,l0,l1)=%d,%d,%d\r\n",
			tag, info, err, phyerr,
			hx_drv_csirx_get_clkln_stopstate(),
			hx_drv_csirx_get_ln0_stopstate(),
			hx_drv_csirx_get_ln1_stopstate());
}

static void csirx_regdump(const char *tag)
{
	xprintf("[REGS %s] CSIRX @0x%08x:\r\n", tag, (uint32_t)CSIRX_REGS_BASE);
	for (uint32_t off = 0; off <= 0x148; off += 4) {
		uint32_t v = *(volatile uint32_t *)(CSIRX_REGS_BASE + off);
		if (v != 0)
			xprintf("  +0x%03x = 0x%08x\r\n", off, v);
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

/* Framework main.c has no OV5647_POC branch; entry lives here. */
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
		app_halt("cisdp_sensor_init failed (is the OV5647 plugged in?)");

	if (cisdp_dp_init(true, CISDP_INIT_TYPE_INP_CROP_1280x960_RAW,
			SENSORDPLIB_PATH_INP_WDMA2, app_dplib_cb, 4) < 0)
		app_halt("cisdp_dp_init failed");

	csirx_signature("pre-start");

	cisdp_sensor_start();

	uint32_t waited = 0;
	while (g_frame_ready == 0 && waited < 5000) {
		hx_drv_timer_cm55x_delay_ms(1, TIMER_STATE_DC);
		waited++;
	}

	csirx_signature(g_frame_ready ? "streaming" : "timeout");
	/* NOTE: csirx_regdump() hangs when the RX is live — do not call here. */
	(void)csirx_regdump;

	if (!g_frame_ready) {
		xprintf("dp_evt=%d\r\n", g_dp_event_err);
		app_halt("no frame from OV5647 in 5s");
	}

	xprintf("frame %d captured, stopping stream for dump\r\n", g_cur_frame);
	cisdp_sensor_stop();

	uint32_t addr = cisdp_get_raw_addr();
	uint32_t w = cisdp_get_raw_width();
	uint32_t h = cisdp_get_raw_height();
	uint32_t size = w * h;	/* WDMA2 raw dump stores 1 byte/pixel */

	hx_InvalidateDCache_by_Addr((volatile void *)addr, size);

	uint32_t crc = crc32_calc((const uint8_t *)addr, size);

	xprintf("\r\n===VD56G3_FRAME_BEGIN len=%d w=%d h=%d bpp=8 crc32=%08x coarse=0===\n",
			size, w, h, crc);
	dump_base64((const uint8_t *)addr, size);
	xprintf("===VD56G3_FRAME_END crc32=%08x===\n", crc);

	xprintf("done — reset board for another capture\r\n");
	while (1) {
		hx_drv_timer_cm55x_delay_ms(1000, TIMER_STATE_DC);
	}
	return 0;
}
