#!/bin/bash
# MuPDF's scripts/hexdump.sh for the small build: dumps the stripped copy of a font
# (build/stripped-fonts/<path>) instead of the original. The C symbol name comes from the
# file name only, so it stays the one MuPDF expects.
exec bash scripts/hexdump.sh "build/stripped-fonts/$1"
