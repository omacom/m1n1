# SPDX-License-Identifier: GPL-2.0-only
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_early_debug_option(tmp_path):
    test = tmp_path / 'test.c'
    test.write_text(r'''
#include "boot_options.h"
#include <assert.h>
#include <string.h>
int main(void) {
    const char *yes[] = {"m1n1-debug", "-v m1n1-debug keepsyms=1", "\tm1n1-debug\r\n", "m1n1-debug m1n1-debug"};
    const char *no[] = {"", "no-m1n1-debug", "m1n1-debugger", "m1n1-debug=0", "other=m1n1-debug", "-v"};
    for (unsigned i=0; i<sizeof(yes)/sizeof(*yes); i++) {
        boot_options_init(yes[i], strlen(yes[i])+1);
        assert(m1n1_debug_enabled());
    }
    for (unsigned i=0; i<sizeof(no)/sizeof(*no); i++) {
        boot_options_init(no[i], strlen(no[i])+1);
        assert(!m1n1_debug_enabled());
    }
    const char proxy[] = "m1n1-proxy m1n1-debug";
    boot_options_init(proxy, sizeof(proxy));
    assert(m1n1_proxy_enabled() && m1n1_debug_enabled());
    boot_options_set_stage1(false);
    assert(!m1n1_proxy_enabled() && m1n1_debug_enabled());
    boot_options_set_stage1(true);
    assert(m1n1_proxy_enabled());
    boot_options_init("m1n1-proxy=0", 13);
    assert(!m1n1_proxy_enabled());
    char truncated[] = {'a','u','r','a','-','d','e','b','u','g'};
    boot_options_init(truncated,sizeof(truncated));
    assert(!m1n1_debug_enabled());
    char native[1024] = {0};
    memset(native,' ',900);
    memcpy(native+900,"m1n1-debug",11);
    boot_options_init(native,sizeof(native));
    assert(m1n1_debug_enabled());
    boot_options_init(native,608);
    assert(!m1n1_debug_enabled());
    boot_options_init(0,0);
    assert(!m1n1_debug_enabled());
    assert(!boot_option_present("m1n1-debug",11,""));
    return 0;
}
''')
    binary = tmp_path/'test'
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + ['-std=c11','-Wall','-Wextra','-Werror','-I',str(ROOT/'src'),str(test),str(ROOT/'src/boot_options.c'),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)


def test_wdt_keep_option(tmp_path):
    test = tmp_path / 'test.c'
    test.write_text(r'''
#include "boot_options.h"
#include <assert.h>
#include <string.h>
int main(void) {
    const char *yes[] = {"m1n1.wdt=keep", "-v m1n1.wdt=keep", "chainload.x=1\tm1n1.wdt=keep\n"};
    const char *no[] = {"", "m1n1.wdt", "m1n1.wdt=", "m1n1.wdt=keeps", "m1n1.wdt=off",
                        "xm1n1.wdt=keep", "m1n1-debug m1n1-proxy"};
    for (unsigned i=0; i<sizeof(yes)/sizeof(*yes); i++) {
        boot_options_init(yes[i], strlen(yes[i])+1);
        assert(m1n1_wdt_keep_enabled());
    }
    for (unsigned i=0; i<sizeof(no)/sizeof(*no); i++) {
        boot_options_init(no[i], strlen(no[i])+1);
        assert(!m1n1_wdt_keep_enabled());
    }
    /* Independent of the stage: a chainloaded stage 2 is where it matters */
    boot_options_init("m1n1.wdt=keep m1n1-proxy", 25);
    boot_options_set_stage1(false);
    assert(m1n1_wdt_keep_enabled() && !m1n1_proxy_enabled());
    char truncated[] = {'m','1','n','1','.','w','d','t','=','k','e','e','p'};
    boot_options_init(truncated, sizeof(truncated));
    assert(!m1n1_wdt_keep_enabled());
    boot_options_init(0, 0);
    assert(!m1n1_wdt_keep_enabled());
    return 0;
}
''')
    binary = tmp_path/'test'
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + ['-std=c11','-Wall','-Wextra','-Werror','-I',str(ROOT/'src'),str(test),str(ROOT/'src/boot_options.c'),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
