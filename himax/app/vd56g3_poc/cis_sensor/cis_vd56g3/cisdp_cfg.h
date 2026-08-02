/*
 * cisdp_cfg.h — ST VD56G3 on Grove Vision AI V2 via ST P-Board (STEVAL-CAM-M0I)
 *
 * POC: full-res packed-RAW10 capture carried as an 8-bit/1400-wide CSI-2 stream.
 * The sensor keeps FORMAT_CTRL=RAW10 (real 10-bit samples, 5 bytes per 4 px on
 * the wire) but advertises MIPI data type RAW8 (OIF_IMG_CTRL 0x030F = 0x2A), so
 * the WE2 datapath byte-copies the packed payload into SRAM untouched.
 * See SENSOR.md in the STEvalEVK-Phone repo for the register provenance.
 */
#ifndef APP_SCENARIO_VD56G3_POC_CISDP_CFG_H_
#define APP_SCENARIO_VD56G3_POC_CISDP_CFG_H_

#include "hx_drv_CIS_common.h"

#ifdef TRUSTZONE_SEC
#ifdef IP_INST_NS_csirx
#define	CSIRX_REGS_BASE 			BASE_ADDR_MIPI_RX_CTRL
#define CSIRX_DPHY_REG				(BASE_ADDR_APB_MIPI_RX_PHY+0x50)
#define CSIRX_DPHY_TUNCATE_REG		(BASE_ADDR_APB_MIPI_RX_PHY+0x48)
#else
#define CSIRX_REGS_BASE             BASE_ADDR_MIPI_RX_CTRL_ALIAS
#define CSIRX_DPHY_REG				(BASE_ADDR_APB_MIPI_RX_PHY_ALIAS+0x50)
#define CSIRX_DPHY_TUNCATE_REG		(BASE_ADDR_APB_MIPI_RX_PHY_ALIAS+0x48)
#endif
#else
#ifndef TRUSTZONE
#define CSIRX_REGS_BASE             BASE_ADDR_MIPI_RX_CTRL_ALIAS
#define CSIRX_DPHY_REG				(BASE_ADDR_APB_MIPI_RX_PHY_ALIAS+0x50)
#define CSIRX_DPHY_TUNCATE_REG		(BASE_ADDR_APB_MIPI_RX_PHY_ALIAS+0x48)
#else
#define CSIRX_REGS_BASE             BASE_ADDR_MIPI_RX_CTRL
#define CSIRX_DPHY_REG				(BASE_ADDR_APB_MIPI_RX_PHY+0x50)
#define CSIRX_DPHY_TUNCATE_REG		(BASE_ADDR_APB_MIPI_RX_PHY+0x48)
#endif
#endif

/* ---- VD56G3 identity / bus ---- */
#define VD56G3_SENSOR_I2CID			(0x10)	/* 7-bit CCI address */
#define VD56G3_MODEL_ID				(0x5603)

/* ---- MIPI link (sensor defaults; the EVK capture never overrode these) ----
 * OIF_CSI_BITRATE default = 1010 Mbps/lane, 2 lanes, continuous clock.
 */
#define VD56G3_MIPI_CLOCK_FEQ		(505)	/* MHz; bitrate_1lane = x2 (DDR) */
#define VD56G3_MIPI_LANE_CNT		(2)
#define VD56G3_MIPI_DPP				(8)		/* receiver-side depth: packed bytes */
#define VD56G3_MIPITX_CNTCLK_EN		(1)

/* ---- Frame geometry as seen by the WE2 (bytes-as-pixels) ----
 * Sensor image: 1120 px RAW10 -> 1400 bytes/row; 1360 rows; ISL disabled.
 */
#define VD56G3_BYTES_PER_ROW		(1400)
#define VD56G3_ROWS					(1360)
#define VD56G3_IMG_WIDTH_PX			(1120)	/* informative (after unpack) */

#define VD56G3_FRAME_SIZE			(VD56G3_BYTES_PER_ROW * VD56G3_ROWS)

/* ---- Sensor timing (SENSOR.md sec.7) ----
 * LINE_LENGTH min 1236 @160.8MHz pixel clock. We stretch x2 so the WE2 INP
 * (draining 1400 B/line at the 96 MHz RC clock = 14.58 us) keeps up with the
 * sensor line period (2472/160.8MHz = 15.37 us). ~30 fps; POC captures one.
 */
#define VD56G3_LINE_LENGTH			(2472)
#define VD56G3_FRAME_LENGTH_LINES	(2168)
/* Manual coarse exposure in line periods: 15 ms / 15.37 us ~= 976 */
#define VD56G3_COARSE_DEFAULT		(976)

/* ---- CIS common plumbing (mirrors stock OV5647 driver on this board) ---- */
#define CIS_I2C_ID					VD56G3_SENSOR_I2CID
#define DEAULT_XHSUTDOWN_PIN		AON_GPIO2

#define SENCTRL_SENSOR_TYPE			SENSORDPLIB_SENSOR_HM2130	/* generic MIPI preset (stock OV5647 driver aliases to this too) */
#define SENCTRL_STREAM_TYPE			SENSORDPLIB_STREAM_NONEAOS
#define SENCTRL_SENSOR_WIDTH		VD56G3_BYTES_PER_ROW
#define SENCTRL_SENSOR_HEIGHT		VD56G3_ROWS

#define DP_INP_OUT_WIDTH			VD56G3_BYTES_PER_ROW
#define DP_INP_OUT_HEIGHT			VD56G3_ROWS

#endif /* APP_SCENARIO_VD56G3_POC_CISDP_CFG_H_ */
