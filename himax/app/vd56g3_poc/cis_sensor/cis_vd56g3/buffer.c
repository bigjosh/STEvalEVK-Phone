/* Frame buffer for one packed-RAW10 frame (1400 B x 1360 rows = 1,904,000 B).
 * Lives in the big SRAM01 region via .bss.NoInit (see vd56g3_poc.ld). */
#include <stdint.h>
#include "WE2_core.h"

__attribute__((section(".bss.NoInit"))) uint8_t raw_buff[(int)1400 * 1360] __ALIGNED(32);
