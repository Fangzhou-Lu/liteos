/* Host stub: kernel printf macros → fprintf(stderr). */
#ifndef _HOST_STUB_LOS_PRINTF_H
#define _HOST_STUB_LOS_PRINTF_H

#include <stdio.h>

#define PRINT_ERR(fmt, ...)   fprintf(stderr, "[ERR]  " fmt, ##__VA_ARGS__)
#define PRINT_WARN(fmt, ...)  fprintf(stderr, "[WARN] " fmt, ##__VA_ARGS__)
#define PRINT_INFO(fmt, ...)  fprintf(stderr, "[INFO] " fmt, ##__VA_ARGS__)
#define PRINTK(fmt, ...)      fprintf(stderr,           fmt, ##__VA_ARGS__)
#define PRINT_DEBUG(fmt, ...) fprintf(stderr, "[DBG]  " fmt, ##__VA_ARGS__)

#endif
