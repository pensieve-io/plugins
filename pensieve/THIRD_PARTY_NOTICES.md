# Third-party notices

## Secret recognition rules

`scripts/secret_masking.py` adapts the capture redaction in OpenCrawl
(https://github.com/getopencrawl/opencrawl-core, `cli/src/capture/redact.ts` at
commit `83b4e1b464cee9695f7f1ed96b91de03e25897b9`), licensed under the Apache
License, Version 2.0. A copy of that licence is in `licenses/Apache-2.0.txt`.
Changes: ported to Python with ASCII word boundaries, uses Pensieve's mask
marker, keeps Pensieve's existing minimums for GitHub and `sk-` keys, adds
formats from Pensieve's own stack, masks credentials after any `Authorization`
scheme, numeric credential fields, command-line flag values and any name
ending in a credential word, and leaves type names, settings references and
calls unmasked.

OpenCrawl's credential formats are adapted from Gitleaks v8.30.0
(https://github.com/gitleaks/gitleaks/tree/v8.30.0).

MIT License

Copyright (c) 2019 Zachary Rice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
