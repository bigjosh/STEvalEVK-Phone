/*
 * cisdp_sensor.c — ST VD56G3 driver for the WE2 datapath (Grove Vision AI V2 + ST P-Board)
 *
 * Init sequence is the hardware-proven EVK cold init (STEvalEVK-Phone repo:
 * firmware/vd56g3_cold_init.json, documented in SENSOR.md). Command registers
 * (0x0200-0x0203) self-clear and MUST be polled to 0; issuing a command while
 * the previous one is nonzero silently drops it.
 *
 * Datapath trick: sensor emits real RAW10 (packed, 1400 B per 1120-px row) but
 * advertises MIPI DT 0x2A (RAW8), so the WE2 stores the packed bytes verbatim
 * as a 1400x1360 8-bit frame. ISL status lines are disabled.
 */

#include "cisdp_sensor.h"
#include "cisdp_cfg.h"
#include "hx_drv_CIS_common.h"
#include "hx_drv_timer.h"
#include "hx_drv_hxautoi2c_mst.h"

#include "WE2_core.h"
#include "WE2_debug.h"
#include "hx_drv_swreg_aon.h"
#include "hx_drv_scu_export.h"
#include "driver_interface.h"
#include "hx_drv_scu.h"
#include "hx_drv_gpio.h"
#include "math.h"

#define GROVE_VISION_AI

extern uint8_t raw_buff[];

static volatile uint32_t g_wdma1_baseaddr;
static volatile uint32_t g_wdma2_baseaddr = 0;
static volatile uint32_t g_wdma3_baseaddr;

/* ---- VD56G3 register map (subset; SENSOR.md) ---- */
#define REG_MODEL_ID			0x0000	/* u16 = 0x5603 */
#define REG_ERROR_CODE			0x001C	/* u16, valid in FSM 0xFF */
#define REG_FWPATCH_REVISION	0x001E	/* u16 */
#define REG_SYSTEM_FSM			0x0028	/* u8: 1=READY_TO_BOOT 2=SW_STANDBY 3=STREAMING 0xFF=ERROR */
#define REG_CMD_BOOT			0x0200	/* <-01 BOOT (self-clearing) */
#define REG_CMD_STANDBY			0x0201	/* <-01 START_STREAM  <-04 THSENS */
#define REG_CMD_STREAMING		0x0202	/* <-01 STOP_STREAM */

#define FSM_READY_TO_BOOT		0x01
#define FSM_SW_STANDBY			0x02
#define FSM_STREAMING			0x03
#define FSM_ERROR				0xFF

/*
 * Plain register writes replayed from the proven cold init (multi-byte values
 * are little-endian: byte i -> addr+i). Command registers are NOT in tables —
 * they need the poll-to-zero handshake, done in C below.
 */
static HX_CIS_SensorSetting_t VD56G3_pll_setting[] = {
	/* analog misc (capture-verbatim, purpose unlisted in UM2602) */
	{HX_CIS_I2C_Action_W, 0x0960, 0x1C},
	{HX_CIS_I2C_Action_W, 0x096A, 0x3C},
	{HX_CIS_I2C_Action_W, 0x096B, 0x00},
	/* EXT_CLOCK u32 = 12,000,000 (P-Board oscillator, marked 12.00) */
	{HX_CIS_I2C_Action_W, 0x0220, 0x00},
	{HX_CIS_I2C_Action_W, 0x0221, 0x1B},
	{HX_CIS_I2C_Action_W, 0x0222, 0xB7},
	{HX_CIS_I2C_Action_W, 0x0223, 0x00},
	/* PLL: prediv 2, mult 134, postdiv 1, VT div 5 -> 160.8 MHz pixel clock */
	{HX_CIS_I2C_Action_W, 0x0224, 0x02},
	{HX_CIS_I2C_Action_W, 0x0226, 0x86},
	{HX_CIS_I2C_Action_W, 0x0225, 0x01},
	{HX_CIS_I2C_Action_W, 0x0227, 0x05},
};

static HX_CIS_SensorSetting_t VD56G3_ctx_setting[] = {
	{HX_CIS_I2C_Action_W, 0x0302, 0x02},	/* ORIENTATION */
	{HX_CIS_I2C_Action_W, 0x044C, 0x02},	/* EXP_MODE = manual (POC: one-shot, no AE warm-up) */
	{HX_CIS_I2C_Action_W, 0x045A, 0x00},	/* Y_START = 0 */
	{HX_CIS_I2C_Action_W, 0x045B, 0x00},
	{HX_CIS_I2C_Action_W, 0x045C, 0x4F},	/* Y_END = 1359 */
	{HX_CIS_I2C_Action_W, 0x045D, 0x05},
	{HX_CIS_I2C_Action_W, 0x0462, 0x00},	/* OUT_ROI_Y_START = 0 */
	{HX_CIS_I2C_Action_W, 0x0463, 0x00},
	{HX_CIS_I2C_Action_W, 0x0464, 0x4F},	/* OUT_ROI_Y_END = 1359 */
	{HX_CIS_I2C_Action_W, 0x0465, 0x05},
	{HX_CIS_I2C_Action_W, 0x0434, 0x00},	/* AE_ROI_START_V = 0 */
	{HX_CIS_I2C_Action_W, 0x0435, 0x00},
	{HX_CIS_I2C_Action_W, 0x0438, 0x4F},	/* AE_ROI_END_V = 1359 */
	{HX_CIS_I2C_Action_W, 0x0439, 0x05},
	{HX_CIS_I2C_Action_W, 0x045E, 0x02},	/* OUT_ROI_X_START = 2 */
	{HX_CIS_I2C_Action_W, 0x045F, 0x00},
	{HX_CIS_I2C_Action_W, 0x0460, 0x61},	/* OUT_ROI_X_END = 1121 -> width 1120 */
	{HX_CIS_I2C_Action_W, 0x0461, 0x04},
	{HX_CIS_I2C_Action_W, 0x0458, 0x78},	/* FRAME_LENGTH = 2168 lines */
	{HX_CIS_I2C_Action_W, 0x0459, 0x08},
};

static HX_CIS_SensorSetting_t VD56G3_poc_setting[] = {
	/* LINE_LENGTH x2 stretch = 2472 px-clocks (WE2 INP drain-rate headroom) */
	{HX_CIS_I2C_Action_W, 0x0300, 0xA8},
	{HX_CIS_I2C_Action_W, 0x0301, 0x09},
	/* MANUAL_COARSE_EXPOSURE = 976 lines (~15 ms at stretched line period) */
	{HX_CIS_I2C_Action_W, 0x044E, 0xD0},
	{HX_CIS_I2C_Action_W, 0x044F, 0x03},
	/* OIF_IMG_CTRL: advertise DT 0x2A (RAW8) while FORMAT_CTRL stays RAW10 */
	{HX_CIS_I2C_Action_W, 0x030F, 0x2A},
	/* ISL_ENABLE = 0: no embedded status lines, frame is pure image rows */
	{HX_CIS_I2C_Action_W, 0x0333, 0x00},
};

int vd56g3_read_u8(uint16_t reg, uint8_t *val)
{
	return (hx_drv_cis_get_reg(reg, val) == HX_CIS_NO_ERROR) ? 0 : -1;
}

int vd56g3_read_u16(uint16_t reg, uint16_t *val)
{
	uint8_t lo = 0, hi = 0;
	if (hx_drv_cis_get_reg(reg, &lo) != HX_CIS_NO_ERROR) return -1;
	if (hx_drv_cis_get_reg(reg + 1, &hi) != HX_CIS_NO_ERROR) return -1;
	*val = (uint16_t)lo | ((uint16_t)hi << 8);
	return 0;
}

/* Self-clearing command: write value, poll same register to 0. */
static int vd56g3_cmd(uint16_t reg, uint8_t val, uint32_t timeout_ms)
{
	if (hx_drv_cis_set_reg(reg, val, 0) != HX_CIS_NO_ERROR) {
		dbg_printf(DBG_LESS_INFO, "VD56G3 cmd 0x%04x<-0x%02x write fail\r\n", reg, val);
		return -1;
	}
	for (uint32_t t = 0; t < timeout_ms; t++) {
		uint8_t v = 0xAA;
		if (hx_drv_cis_get_reg(reg, &v) == HX_CIS_NO_ERROR && v == 0)
			return 0;
		hx_drv_timer_cm55x_delay_ms(1, TIMER_STATE_DC);
	}
	dbg_printf(DBG_LESS_INFO, "VD56G3 cmd 0x%04x<-0x%02x not acked in %dms\r\n", reg, val, timeout_ms);
	return -1;
}

static int vd56g3_wait_fsm(uint8_t target, uint32_t timeout_ms)
{
	uint8_t fsm = 0;
	for (uint32_t t = 0; t < timeout_ms; t++) {
		if (hx_drv_cis_get_reg(REG_SYSTEM_FSM, &fsm) == HX_CIS_NO_ERROR) {
			if (fsm == target)
				return 0;
			if (fsm == FSM_ERROR) {
				uint16_t err = 0;
				vd56g3_read_u16(REG_ERROR_CODE, &err);
				dbg_printf(DBG_LESS_INFO, "VD56G3 FSM=ERROR, ERROR_CODE=0x%04x\r\n", err);
				return -2;
			}
		}
		hx_drv_timer_cm55x_delay_ms(1, TIMER_STATE_DC);
	}
	dbg_printf(DBG_LESS_INFO, "VD56G3 FSM wait %d timeout (last=%d)\r\n", target, fsm);
	return -1;
}

/* 200 MHz MIPI RX clock from PLL — same as the stock IMX219 driver (912 Mbps
 * lane rate, closest analog to our 1010). RC96 was not enough: EDM WDT2
 * timeouts, no frames. */
static void vd56g3_set_dp_pll200(void)
{
	SCU_PDHSC_DPCLK_CFG_T cfg;

	hx_drv_scu_get_pdhsc_dpclk_cfg(&cfg);

	uint32_t pllfreq;
	hx_drv_swreg_aon_get_pllfreq(&pllfreq);

	if (pllfreq == 400000000) {
		cfg.mipiclk.hscmipiclksrc = SCU_HSCMIPICLKSRC_PLL;
		cfg.mipiclk.hscmipiclkdiv = 1;
	} else {
		cfg.mipiclk.hscmipiclksrc = SCU_HSCMIPICLKSRC_PLL;
		cfg.mipiclk.hscmipiclkdiv = 0;
	}

	hx_drv_scu_set_pdhsc_dpclk_cfg(cfg, 0, 1);

	uint32_t mipi_pixel_clk = 96;
	hx_drv_scu_get_freq(SCU_CLK_FREQ_TYPE_HSC_MIPI_RXCLK, &mipi_pixel_clk);
	mipi_pixel_clk = mipi_pixel_clk / 1000000;

	dbg_printf(DBG_LESS_INFO, "MIPI RX CLK (PLL src): %dM (pll %d)\n", mipi_pixel_clk, pllfreq);
}

static uint32_t g_cur_bitrate = VD56G3_MIPI_CLOCK_FEQ * 2;
static uint8_t  g_cur_hscnt = 0x10;
static uint8_t  g_cur_lanes = VD56G3_MIPI_LANE_CNT;

static void set_mipi_csirx_enable(void)
{
	uint32_t bitrate_1lane = g_cur_bitrate;
	uint32_t mipi_lnno = g_cur_lanes;
	uint32_t pixel_dpp = VD56G3_MIPI_DPP;
	uint32_t line_length = VD56G3_BYTES_PER_ROW;
	uint32_t frame_length = VD56G3_ROWS;
	uint32_t byte_clk = bitrate_1lane / 8;
	uint32_t continuousout = VD56G3_MIPITX_CNTCLK_EN;
	uint32_t deskew_en = 0;
	uint32_t mipi_pixel_clk = 96;

	vd56g3_set_dp_pll200();

	hx_drv_scu_get_freq(SCU_CLK_FREQ_TYPE_HSC_MIPI_RXCLK, &mipi_pixel_clk);
	mipi_pixel_clk = mipi_pixel_clk / 1000000;

	dbg_printf(DBG_LESS_INFO, "MIPI CSI Init Enable\n");
	dbg_printf(DBG_LESS_INFO, "MIPI BITRATE 1LANE: %dM, LANES: %d, DPP: %d\n", bitrate_1lane, mipi_lnno, pixel_dpp);
	dbg_printf(DBG_LESS_INFO, "MIPI LINE(bytes): %d, FRAME(rows): %d\n", line_length, frame_length);

	uint32_t n_preload = 15;
	uint32_t l_header = 4;
	uint32_t l_footer = 2;

	double t_input = (double)(l_header + line_length * pixel_dpp / 8 + l_footer) / (mipi_lnno * byte_clk) + 0.06;
	double t_output = (double)line_length / mipi_pixel_clk;
	double t_preload = (double)(7 + (n_preload * 4) / mipi_lnno) / mipi_pixel_clk;

	double delta_t = t_input - t_output - t_preload;

	uint16_t rx_fifo_fill = 0;
	uint16_t tx_fifo_fill = 0;

	if (delta_t <= 0) {
		delta_t = 0 - delta_t;
		tx_fifo_fill = ceil(delta_t * byte_clk * mipi_lnno / 4 / (pixel_dpp / 2)) * (pixel_dpp / 2);
		rx_fifo_fill = 0;
	} else {
		rx_fifo_fill = ceil(delta_t * byte_clk * mipi_lnno / 4 / (pixel_dpp / 2)) * (pixel_dpp / 2);
		tx_fifo_fill = 0;
	}
	dbg_printf(DBG_LESS_INFO, "MIPI RX FIFO FILL: %d, TX FIFO FILL: %d\n", rx_fifo_fill, tx_fifo_fill);

	/* Reset CSI RX/TX */
	SCU_DP_SWRESET_T dp_swrst;
	drv_interface_get_dp_swreset(&dp_swrst);
	dp_swrst.HSC_MIPIRX = 0;
	dp_swrst.HSC_MIPITX = 0;

	hx_drv_scu_set_DP_SWReset(dp_swrst);
	hx_drv_timer_cm55x_delay_us(50, TIMER_STATE_DC);

	dp_swrst.HSC_MIPIRX = 1;
	dp_swrst.HSC_MIPITX = 1;
	hx_drv_scu_set_DP_SWReset(dp_swrst);

	MIPIRX_DPHYHSCNT_CFG_T hscnt_cfg;
	hscnt_cfg.mipirx_dphy_hscnt_clk_en = 0;
	hscnt_cfg.mipirx_dphy_hscnt_ln0_en = 1;
	hscnt_cfg.mipirx_dphy_hscnt_ln1_en = 1;

	hscnt_cfg.mipirx_dphy_hscnt_clk_val = 0x03;
	hscnt_cfg.mipirx_dphy_hscnt_ln0_val = g_cur_hscnt;
	hscnt_cfg.mipirx_dphy_hscnt_ln1_val = g_cur_hscnt;
	sensordplib_csirx_set_hscnt(hscnt_cfg);
	dbg_printf(DBG_LESS_INFO, "hscnt=0x%02x\n", g_cur_hscnt);

	if (pixel_dpp == 10 || pixel_dpp == 8) {
		sensordplib_csirx_set_pixel_depth(pixel_dpp);
	} else {
		dbg_printf(DBG_LESS_INFO, "PIXEL DEPTH fail %d\n", pixel_dpp);
		return;
	}

	sensordplib_csirx_set_deskew(deskew_en);
	sensordplib_csirx_set_fifo_fill(rx_fifo_fill);
	sensordplib_csirx_enable(mipi_lnno);

	(void)continuousout;
	dbg_printf(DBG_LESS_INFO, "MIPI CSI RX enabled\n");
}

static void set_mipi_csirx_disable(void)
{
	dbg_printf(DBG_LESS_INFO, "MIPI CSI Disable\n");
	sensordplib_csirx_disable();
}

int cisdp_sensor_init(void)
{
	dbg_printf(DBG_LESS_INFO, "cis_VD56G3_init\r\n");

	hx_drv_cis_init((CIS_XHSHUTDOWN_INDEX_E)DEAULT_XHSUTDOWN_PIN, SENSORCTRL_MCLK_DIV3);
	dbg_printf(DBG_LESS_INFO, "mclk DIV3, xshutdown_pin=%d\n", DEAULT_XHSUTDOWN_PIN);

#ifdef GROVE_VISION_AI
	/*
	 * PA1 (AON_GPIO1) drives the FFC cam-GPIO -> P-Board NRST -> sensor XSDN.
	 * Hold low >=100 ms, release, then wait for the sensor MCU to boot
	 * (I2C answering IS the ready signal — SENSOR.md sec.2).
	 */
	hx_drv_gpio_set_output(AON_GPIO1, GPIO_OUT_LOW);
	hx_drv_scu_set_PA1_pinmux(SCU_PA1_PINMUX_AON_GPIO1, 1);
	hx_drv_gpio_set_out_value(AON_GPIO1, GPIO_OUT_LOW);
	hx_drv_timer_cm55x_delay_ms(100, TIMER_STATE_DC);
	hx_drv_gpio_set_out_value(AON_GPIO1, GPIO_OUT_HIGH);
	dbg_printf(DBG_LESS_INFO, "XSHUTDOWN released via PA1(AON_GPIO1)\n");
#else
	hx_drv_sensorctrl_set_xSleepCtrl(SENSORCTRL_XSLEEP_BY_CPU);
	hx_drv_sensorctrl_set_xSleep(1);
#endif

	hx_drv_cis_set_slaveID(CIS_I2C_ID);
	dbg_printf(DBG_LESS_INFO, "hx_drv_cis_set_slaveID(0x%02X)\n", CIS_I2C_ID);

	/* Sensor MCU boot time after XSHUTDOWN release depends on CLKIN; the I2C
	 * block may ACK with zeros before READY_TO_BOOT. Poll until MODEL_ID
	 * reads correctly (not merely until a read succeeds). */
	hx_drv_timer_cm55x_delay_ms(50, TIMER_STATE_DC);

	uint16_t model = 0;
	int ok = -1;
	int nacks = 0, zeros = 0;
	for (int i = 0; i < 200; i++) {
		if (vd56g3_read_u16(REG_MODEL_ID, &model) != 0) {
			nacks++;
		} else if (model == VD56G3_MODEL_ID) {
			ok = 0;
			dbg_printf(DBG_LESS_INFO, "MODEL_ID=0x5603 after %dms (nacks=%d zeros=%d)\r\n",
					50 + i * 10, nacks, zeros);
			break;
		} else {
			zeros++;
			if (zeros <= 3 || (zeros % 50) == 0) {
				uint8_t f = 0xEE;
				vd56g3_read_u8(REG_SYSTEM_FSM, &f);
				dbg_printf(DBG_LESS_INFO, "  probe %d: MODEL_ID=0x%04X FSM=0x%02X\r\n", i, model, f);
			}
		}
		hx_drv_timer_cm55x_delay_ms(10, TIMER_STATE_DC);
	}
	if (ok != 0) {
		uint8_t b0 = 0xEE, b1 = 0xEE, fsm2 = 0xEE;
		uint16_t rev = 0;
		vd56g3_read_u8(0x0000, &b0);
		vd56g3_read_u8(0x0001, &b1);
		vd56g3_read_u8(REG_SYSTEM_FSM, &fsm2);
		vd56g3_read_u16(0x0002, &rev);
		dbg_printf(DBG_LESS_INFO,
				"VD56G3 probe FAILED after 2s: model=0x%04X bytes=%02x/%02x FSM=0x%02X rev=0x%04X nacks=%d zeros=%d\r\n",
				model, b0, b1, fsm2, rev, nacks, zeros);
		return -1;
	}

	uint8_t fsm = 0;
	vd56g3_read_u8(REG_SYSTEM_FSM, &fsm);
	dbg_printf(DBG_LESS_INFO, "SYSTEM_FSM=%d (1=READY_TO_BOOT)\r\n", fsm);

	uint16_t fwrev = 0;
	vd56g3_read_u16(REG_FWPATCH_REVISION, &fwrev);
	dbg_printf(DBG_LESS_INFO, "FWPATCH_REVISION=0x%04X (0 = unpatched, fine)\r\n", fwrev);

	if (fsm == FSM_READY_TO_BOOT) {
		/* BOOT command, then SW_STANDBY */
		if (vd56g3_cmd(REG_CMD_BOOT, 0x01, 200) != 0)
			return -1;
		if (vd56g3_wait_fsm(FSM_SW_STANDBY, 500) != 0)
			return -1;
		dbg_printf(DBG_LESS_INFO, "BOOT ok, FSM=SW_STANDBY\r\n");
	} else if (fsm != FSM_SW_STANDBY) {
		dbg_printf(DBG_LESS_INFO, "VD56G3: unexpected FSM %d\r\n", fsm);
		return -1;
	}

	/* Clock/PLL (SW_STANDBY only), context 0, POC deltas */
	if (hx_drv_cis_setRegTable(VD56G3_pll_setting, HX_CIS_SIZE_N(VD56G3_pll_setting, HX_CIS_SensorSetting_t)) != HX_CIS_NO_ERROR) {
		dbg_printf(DBG_LESS_INFO, "VD56G3 PLL table fail\r\n");
		return -1;
	}
	if (hx_drv_cis_setRegTable(VD56G3_ctx_setting, HX_CIS_SIZE_N(VD56G3_ctx_setting, HX_CIS_SensorSetting_t)) != HX_CIS_NO_ERROR) {
		dbg_printf(DBG_LESS_INFO, "VD56G3 CTX table fail\r\n");
		return -1;
	}
	if (hx_drv_cis_setRegTable(VD56G3_poc_setting, HX_CIS_SIZE_N(VD56G3_poc_setting, HX_CIS_SensorSetting_t)) != HX_CIS_NO_ERROR) {
		dbg_printf(DBG_LESS_INFO, "VD56G3 POC table fail\r\n");
		return -1;
	}
	dbg_printf(DBG_LESS_INFO, "VD56G3 configured (1120x1360 RAW10 packed as 1400x1360 DT=RAW8, ISL off, LL=2472, coarse=976)\r\n");

	return 0;
}

static void cisdp_wdma_addr_init(void)
{
	g_wdma1_baseaddr = (uint32_t)raw_buff;
	g_wdma2_baseaddr = (uint32_t)raw_buff;
	g_wdma3_baseaddr = (uint32_t)raw_buff;

	sensordplib_set_xDMA_baseaddrbyapp(g_wdma1_baseaddr, g_wdma2_baseaddr, g_wdma3_baseaddr);
	dbg_printf(DBG_LESS_INFO, "WDMA2 (raw) addr=0x%x size=%d\n", g_wdma2_baseaddr, VD56G3_FRAME_SIZE);
}

int cisdp_dp_init(bool inp_init, CISDP_INIT_TYPE_E type, SENSORDPLIB_PATH_E dp_type, sensordplib_CBEvent_t dplib_cb)
{
	(void)type;

	cisdp_wdma_addr_init();

	set_mipi_csirx_enable();

	INP_CROP_T crop;
	crop.start_x = 0;
	crop.start_y = 0;
	crop.last_x = VD56G3_BYTES_PER_ROW - 1;
	crop.last_y = VD56G3_ROWS - 1;

	if (inp_init) {
		sensordplib_set_sensorctrl_inp_wi_crop(SENCTRL_SENSOR_TYPE, SENCTRL_STREAM_TYPE,
			SENCTRL_SENSOR_WIDTH, SENCTRL_SENSOR_HEIGHT, INP_SUBSAMPLE_DISABLE, crop);
	}

	if (dp_type != SENSORDPLIB_PATH_INP_WDMA2) {
		dbg_printf(DBG_LESS_INFO, "vd56g3_poc supports only PATH_INP_WDMA2\r\n");
		return -1;
	}

	sensordplib_set_raw_wdma2(VD56G3_BYTES_PER_ROW, VD56G3_ROWS, dplib_cb);

	return 0;
}

int cisdp_sensor_start(void)
{
	/* START_STREAM (0x0201<-01), poll to 0, then FSM must reach STREAMING. */
	if (vd56g3_cmd(REG_CMD_STANDBY, 0x01, 500) != 0)
		return -1;
	if (vd56g3_wait_fsm(FSM_STREAMING, 1000) != 0)
		return -1;
	dbg_printf(DBG_LESS_INFO, "VD56G3 STREAMING\r\n");

	sensordplib_set_mclkctrl_xsleepctrl_bySCMode();
	sensordplib_set_sensorctrl_start();

	return 0;
}

void cisdp_sensor_stop(void)
{
	sensordplib_stop_capture();
	sensordplib_start_swreset();
	sensordplib_stop_swreset_WoSensorCtrl();

	/* STOP_STREAM (0x0202<-01); sensor finishes the in-flight frame first. */
	if (vd56g3_cmd(REG_CMD_STREAMING, 0x01, 500) == 0)
		vd56g3_wait_fsm(FSM_SW_STANDBY, 1000);
	dbg_printf(DBG_LESS_INFO, "VD56G3 stopped (SW_STANDBY)\r\n");

	set_mipi_csirx_disable();
}

uint32_t app_get_raw_addr(void)   { return (uint32_t)&raw_buff[0]; }
uint32_t app_get_raw_sz(void)     { return VD56G3_FRAME_SIZE; }
uint32_t app_get_raw_width(void)  { return VD56G3_BYTES_PER_ROW; }
uint32_t app_get_raw_height(void) { return VD56G3_ROWS; }

/*
 * Link-bring-up matrix support: reconfigure sensor-side CSI params (STATIC
 * group -> SW_STANDBY only) and re-init the WE2 CSI-RX, without a reboot.
 */
int cisdp_set_link(uint16_t bitrate_mbps, uint16_t oif_raw)
{
	uint8_t fsm = 0;
	vd56g3_read_u8(REG_SYSTEM_FSM, &fsm);
	if (fsm == FSM_STREAMING) {
		if (vd56g3_cmd(REG_CMD_STREAMING, 0x01, 500) != 0) return -1;
		if (vd56g3_wait_fsm(FSM_SW_STANDBY, 1000) != 0) return -1;
	}

	/* OIF_CSI_BITRATE (0x0312, u16 LE, Mbps per lane) */
	hx_drv_cis_set_reg(0x0312, bitrate_mbps & 0xFF, 0);
	hx_drv_cis_set_reg(0x0313, bitrate_mbps >> 8, 0);

	/* OIF_CTRL (0x0308, u16) written wholesale:
	 * bits2:0 DATALANE_NB, bit3 CLKLANE_SWAP, bits5:4 DATALANE0_MAPPING,
	 * bit6 DATALANE0_SWAP, bits8:7 DATALANE1_MAPPING, bit9 DATALANE1_SWAP.
	 * Power-on default reads 0x0240 (both lane polarities swapped). */
	uint16_t oif = 0;
	vd56g3_read_u16(0x0308, &oif);
	hx_drv_cis_set_reg(0x0308, oif_raw & 0xFF, 0);
	hx_drv_cis_set_reg(0x0309, oif_raw >> 8, 0);

	uint16_t rb = 0;
	vd56g3_read_u16(0x0308, &rb);
	dbg_printf(DBG_LESS_INFO, "link: bitrate=%d OIF_CTRL 0x%04x->0x%04x (rb 0x%04x)\r\n",
			bitrate_mbps, oif, oif_raw, rb);
	return 0;
}

void cisdp_csirx_reinit(uint16_t bitrate_mbps, uint8_t hscnt, uint8_t lanes)
{
	g_cur_bitrate = bitrate_mbps;
	g_cur_hscnt = hscnt;
	g_cur_lanes = lanes;
	sensordplib_csirx_disable();
	set_mipi_csirx_enable();
}
