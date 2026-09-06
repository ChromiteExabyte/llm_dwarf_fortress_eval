# Licensing and third-party notices

## This project

Except for third-party material carrying its own notice, this project's code
and documentation are free software: you may redistribute and modify them
under the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or, at your option, any later
version. The SPDX license identifier is `GPL-3.0-or-later`.

This project is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full terms.

Copyright remains with the respective copyright holders. This grant does not
replace the terms of third-party works or the license documents themselves.

The complete, unmodified GPLv3 text in `LICENSE` was obtained on 2026-09-05
from the [SPDX license-list-data repository](https://raw.githubusercontent.com/spdx/license-list-data/main/text/GPL-3.0-or-later.txt),
after the GNU text endpoint was unavailable. Its SHA256 is
`fb981668c18a279e285fc4d83fba1e836cc84dd4daa73c9697d3cfd2d8aca6e0`.
See also the [GNU GPLv3 reference](https://www.gnu.org/licenses/gpl-3.0.html)
and [SPDX license entry](https://spdx.org/licenses/GPL-3.0-or-later.html).

## Dwarf Fortress

[Dwarf Fortress](https://www.bay12games.com/dwarves/) is a separate,
proprietary game from Bay 12 Games, created by Tarn and Zach Adams. Obtain and
install it separately under the terms supplied with your copy. This
repository does not distribute the game executable, its assets, or saves.
The project's GPL grant does not grant rights to those materials.

## DFHack and optional dependencies

[DFHack](https://github.com/DFHack/dfhack) is a separate project installed by
the operator. Its code and bundled components have their own upstream
licenses; consult the [DFHack license notices](https://github.com/DFHack/dfhack/blob/53.16-r1.1/LICENSE.rst)
and the notices supplied with the release you install. The setup helper
downloads an official DFHack archive into local, ignored directories.
DFHack binaries and bundled assets are not part of this repository's source
distribution.

The optional [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)
and development dependencies are installed separately by the package manager
under their respective licenses. They are not vendored here. Use of an
external model service is also subject to that service's terms.

## DFHack source adaptations and API references

The construction finalization sequence in
[`dfeval-prepare.lua`](src/dfeval/bridge/lua/dfeval-prepare.lua) is adapted
from DFHack's
[`build-now.lua`](https://github.com/DFHack/scripts/blob/7549711a993e03bef19e90b27427096c1099853e/build-now.lua#L263-L293),
at the scripts revision bundled with DFHack 53.16-r1.1. This is an altered,
restricted adaptation for one building created by the fixture. It is not
the original DFHack script. Its source header also identifies the adaptation.

The brewing instrumentation in
[`dfeval-live.lua`](src/dfeval/bridge/lua/dfeval-live.lua) was implemented
against the pinned DFHack
[`eventful` reaction callbacks](https://github.com/DFHack/dfhack/blob/b638b59d0876d9bdf8b5f97e52714206ab7f3266/plugins/eventful.cpp#L328-L359)
and
[`workshops` helpers](https://github.com/DFHack/dfhack/blob/b638b59d0876d9bdf8b5f97e52714206ab7f3266/library/lua/dfhack/workshops.lua).
These links credit the API implementation used to establish the callback
semantics; they do not claim that the upstream C++ implementation is bundled.

The [pinned DFHack license notice](https://github.com/DFHack/dfhack/blob/b638b59d0876d9bdf8b5f97e52714206ab7f3266/LICENSE.rst)
states that its core, plugins, and scripts use the Zlib license unless
otherwise noted, and lists DFHack copyright as (c) 2009-2012, Petr Mrázek.
Copyright in contributed work remains with its respective holders. The
following upstream Zlib notice is preserved unchanged for the adapted
DFHack material; the project's GPL grant does not replace that notice.

```text
This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any
damages arising from the use of this software.

Permission is granted to anyone to use this software for any
purpose, including commercial applications, and to alter it and
redistribute it freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must
   not claim that you wrote the original software. If you use this
   software in a product, an acknowledgment in the product
   documentation would be appreciated but is not required.

2. Altered source versions must be plainly marked as such, and
   must not be misrepresented as being the original software.

3. This notice may not be removed or altered from any source
   distribution.
```

## Earlier bridge transport attribution

The inherited header of
[`dfeval-bridge.lua`](src/dfeval/bridge/lua/dfeval-bridge.lua) credits
[`xuruiyang/df-ai-agent`](https://github.com/xuruiyang/df-ai-agent) for the
resident Lua script and JSON-file transport pattern. That acknowledgment is
preserved. It establishes the stated design influence; it does not establish
which, if any, implementation text was copied or adapted.

For provenance, the upstream project's
[published license notice](https://github.com/xuruiyang/df-ai-agent/blob/develop/LICENSE)
is reproduced below, unchanged. It applies to the upstream work and any
material derived from it, rather than replacing this project's GPL grant.

```text
This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any
damages arising from the use of this software.

Permission is granted to anyone to use this software for any
purpose, including commercial applications, and to alter it and
redistribute it freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must
   not claim that you wrote the original software. If you use this
   software in a product, an acknowledgment in the product
   documentation would be appreciated but is not required.

2. Altered source versions must be plainly marked as such, and
   must not be misrepresented as being the original software.

3. This notice may not be removed or altered from any source
   distribution.
```

## Names and endorsement

Names of third-party games, projects, organizations, and people identify the
works discussed or used. This is an independent project. No endorsement by
Bay 12 Games, Tarn or Zach Adams, DFHack contributors, the Free Software
Foundation, Richard Stallman, Anthropic, or the credited prior-art authors is
claimed.
