# AI-assisted contributions

This is omacom's m1n1, the bootloader behind Aurora and Omarchy on Apple Silicon. Unlike upstream Asahi Linux, this repository accepts changes written with AI tools.

- A person is responsible for every change. They review it, test it on the hardware it affects, and sign it off with `Signed-off-by:`.
- Disclose AI help with a `Co-Authored-By:` trailer naming the tool and model, for example `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. A `Claude-Session:` link is optional.
- Say in the commit message what was tested, and on which Mac and firmware.

Changes meant for upstream [AsahiLinux/m1n1](https://github.com/AsahiLinux/m1n1) must follow upstream's policy, which does not accept AI-generated contributions: https://asahilinux.org/slop/
