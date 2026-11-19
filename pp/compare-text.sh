#!/usr/bin/env bash
# Compare MuPDF text extraction of two mutool builds, every page of every PDF.
#
#   pp/compare-text.sh <mutool-reference> <mutool-candidate> <pdf-list> <out-dir> [jobs]
#
# Each PDF is converted to structured text (characters, quads, origins, font names and
# sizes) by both builds. Identical output is counted; a difference keeps both outputs in
# <out-dir>/diffs/<n>/. <out-dir>/results.tsv lists one line per PDF:
#   same|differ <TAB> exit codes <TAB> characters extracted <TAB> pdf
# A nonzero exit code means mutool failed on at least part of the file.
set -u

REF=$(realpath "$1")
NEW=$(realpath "$2")
LIST=$3
OUT=$4
JOBS=${5:-8}

mkdir -p "$OUT/diffs"
: > "$OUT/results.tsv"

one() {
	pdf=$1
	tmp=$(mktemp -d)
	# Limit memory per process so one huge file cannot exhaust the machine.
	( ulimit -v 4000000; timeout 300 "$REF" draw -q -F stext -o "$tmp/ref.xml" "$pdf" ) 2>/dev/null
	ref_status=$?
	( ulimit -v 4000000; timeout 300 "$NEW" draw -q -F stext -o "$tmp/new.xml" "$pdf" ) 2>/dev/null
	new_status=$?
	chars=$(grep -c '<char ' "$tmp/ref.xml" 2>/dev/null)
	if [ $ref_status -ne $new_status ]; then
		result=differ
	elif cmp -s "$tmp/ref.xml" "$tmp/new.xml"; then
		result=same
	else
		result=differ
	fi
	if [ $result = differ ]; then
		d="$OUT/diffs/$(echo "$pdf" | md5sum | cut -c1-12)"
		mkdir -p "$d"
		echo "$pdf ref=$ref_status new=$new_status" > "$d/source.txt"
		mv "$tmp/ref.xml" "$tmp/new.xml" "$d/" 2>/dev/null
	fi
	rm -rf "$tmp"
	printf '%s\t%s/%s\t%s\t%s\n' "$result" "$ref_status" "$new_status" "${chars:-0}" "$pdf"
}
export -f one
export REF NEW OUT

tr '\n' '\0' < "$LIST" | xargs -0 -P "$JOBS" -I{} bash -c 'one "$1"' _ {} >> "$OUT/results.tsv"

awk -F'\t' '{ n[$1 ($2 == "0/0" ? "" : " (mutool error)")]++; c += $3 }
	END { for (k in n) print n[k], k; print c, "characters compared" }' "$OUT/results.tsv"
