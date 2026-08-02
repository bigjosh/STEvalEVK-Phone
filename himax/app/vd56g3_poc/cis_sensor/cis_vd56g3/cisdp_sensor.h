#ifndef APP_SCENARIO_VD56G3_POC_CISDP_SENSOR_H_
#define APP_SCENARIO_VD56G3_POC_CISDP_SENSOR_H_

#include "cisdp_cfg.h"
#include "WE2_device.h"
#include "hx_drv_scu_export.h"
#include "hx_drv_scu.h"
#include "sensor_dp_lib.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum CISDP_INIT_TYPE_S
{
	CISDP_INIT_TYPE_FULL_RAW = 0x00,	/* 1400x1360 packed-RAW10-as-RAW8 -> WDMA2 */
} CISDP_INIT_TYPE_E;

int  cisdp_sensor_init(void);
int  cisdp_dp_init(bool inp_init, CISDP_INIT_TYPE_E type, SENSORDPLIB_PATH_E dp_type, sensordplib_CBEvent_t dplib_cb);
int  cisdp_sensor_start(void);
void cisdp_sensor_stop(void);

uint32_t app_get_raw_addr(void);
uint32_t app_get_raw_sz(void);
uint32_t app_get_raw_width(void);
uint32_t app_get_raw_height(void);

/* Diagnostics (valid after cisdp_sensor_init) */
int vd56g3_read_u8(uint16_t reg, uint8_t *val);
int vd56g3_read_u16(uint16_t reg, uint16_t *val);

/* Link bring-up matrix: sensor-side CSI params + WE2 RX re-init (no reboot) */
int  cisdp_set_link(uint16_t bitrate_mbps, uint16_t oif_raw);
void cisdp_csirx_reinit(uint16_t bitrate_mbps, uint8_t hscnt, uint8_t lanes);

#ifdef __cplusplus
}
#endif

#endif /* APP_SCENARIO_VD56G3_POC_CISDP_SENSOR_H_ */
