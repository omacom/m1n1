/* SPDX-License-Identifier: MIT */

#ifndef PROBE_T6032_H
#define PROBE_T6032_H

/* On-screen T6032 (Mac Studio) probe: runs at boot on this chip only, draws
 * its findings in large text on the firmware framebuffer, then returns so
 * the normal proxy path runs. Never boots anything. */

void probe_t6032_run(void);

/* Renders the same screen with made-up numbers labelled DEMO. Only called
 * from the PROBE_FB_DEMO build on non-T6032 boards (the M6 bring-up path). */
void probe_fb_demo_run(void);

#endif
