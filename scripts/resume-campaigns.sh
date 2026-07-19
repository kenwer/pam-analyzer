#!/bin/bash
set -euo pipefail

# Resume an interrupted analysis by processing only the campaigns that have no
# detections CSV yet, without touching the ones already finished.
#
# A PAM Analyzer project is a self-contained folder (pam-analyzer.toml plus one
# subfolder per campaign), and a campaign is self-contained too (its own
# campaign toml, species_list.txt, and output CSVs all live inside it). So the
# unfinished campaigns can be split into a temporary sibling project, analyzed
# there in the GUI, then merged back.
#
# Usage:
#   scripts/resume-campaigns.sh split <project-dir> [temp-dir] [--model KEY]
#   scripts/resume-campaigns.sh merge <project-dir> [temp-dir] [--model KEY]
#
#   split  Copies the project toml into <temp-dir> and moves every campaign
#          that lacks its detections CSV from <project-dir> into <temp-dir>.
#          Open <temp-dir> in PAM Analyzer and run "All campaigns".
#   merge  Moves every campaign back from <temp-dir> into <project-dir>, then
#          removes the temp project.
#
# <temp-dir> defaults to "<project-dir>_RESUME". It MUST be on the same volume
# as the project so the moves are instant metadata renames, not multi-GB
# copies of the audio. The script refuses to run otherwise.
#
# The model whose CSV marks a campaign complete defaults to BirdNET-2.4. Pick
# the other with --model, e.g. --model Perch-2.0. Known models: BirdNET-2.4,
# Perch-2.0. The RESUME_MODEL env var sets the default when no --model is given.
# Both subcommands need the same --model, so split and merge agree on which CSV
# means "done".
#
# Note: adequate power is a prerequisite for the actual run. A run pegs all CPU
# cores, so power the Mac from a full-wattage charger, not a display's USB-C.
# An underpowered Mac can drain to a critical-battery sleep that drops the USB
# drive mid-read and aborts the run. This script is crash-resilient though: if
# a run dies partway, run split again to move any still-missing campaigns, or
# just re-run in the GUI, since finished campaigns are skipped by the CSV check.

PROJECT_TOML="pam-analyzer.toml"
KNOWN_MODELS=("BirdNET-2.4" "Perch-2.0")

die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 {split|merge} <project-dir> [temp-dir] [--model KEY]"
  echo "  --model KEY   one of: ${KNOWN_MODELS[*]} (default: BirdNET-2.4)"
}

# Reject an unknown model so a typo cannot make split treat finished campaigns
# as unfinished (a wrong CSV name matches nothing, so every folder looks
# missing and would be moved).
assert_known_model() {
  local m="$1" k
  for k in "${KNOWN_MODELS[@]}"; do
    [ "$m" = "$k" ] && return 0
  done
  die "unknown model: $m (expected one of: ${KNOWN_MODELS[*]})"
}

# Fail unless both paths resolve to the same filesystem, so a move is a rename.
assert_same_volume() {
  local a="$1" b="$2"
  local dev_a dev_b
  dev_a="$(stat -f '%d' "$a")" || die "cannot stat $a"
  dev_b="$(stat -f '%d' "$b")" || die "cannot stat $b"
  if [ "$dev_a" != "$dev_b" ]; then
    die "temp dir is on a different volume than the project. Moves would copy every audio file. Choose a temp dir on the same drive."
  fi
}

cmd_split() {
  local src="$1" tmp="$2"
  [ -d "$src" ] || die "project dir not found: $src"
  [ -f "$src/$PROJECT_TOML" ] || die "no $PROJECT_TOML in $src (not a project?)"

  # Guard against the parent of tmp, since tmp itself may not exist yet.
  local tmp_parent
  tmp_parent="$(dirname "$tmp")"
  [ -d "$tmp_parent" ] || die "parent of temp dir does not exist: $tmp_parent"
  assert_same_volume "$src" "$tmp_parent"

  mkdir -p "$tmp"
  cp -n "$src/$PROJECT_TOML" "$tmp/$PROJECT_TOML"

  shopt -s nullglob
  local moved=0
  for d in "$src"/*/; do
    d="${d%/}"
    if [ ! -f "$d/$CSV_NAME" ]; then
      echo "  move -> $(basename "$d")"
      mv "$d" "$tmp/"
      moved=$((moved + 1))
    fi
  done
  shopt -u nullglob

  if [ "$moved" -eq 0 ]; then
    echo "No campaigns without $CSV_NAME. Nothing to split."
  else
    echo "Split $moved campaign(s) into: $tmp"
    echo "Open that folder in PAM Analyzer, run All campaigns, then: merge"
  fi
}

cmd_merge() {
  local src="$1" tmp="$2"
  [ -d "$tmp" ] || die "temp dir not found: $tmp"
  [ -d "$src" ] || die "project dir not found: $src"
  assert_same_volume "$src" "$tmp"

  shopt -s nullglob
  local moved=0
  for d in "$tmp"/*/; do
    d="${d%/}"
    local name
    name="$(basename "$d")"
    if [ -e "$src/$name" ]; then
      echo "  SKIP (already in project): $name" >&2
      continue
    fi
    echo "  move <- $name"
    mv "$d" "$src/"
    moved=$((moved + 1))
  done
  shopt -u nullglob

  echo "Merged $moved campaign(s) back into: $src"

  # Remove the temp project only if nothing but the copied toml remains.
  rm -f "$tmp/$PROJECT_TOML"
  if rmdir "$tmp" 2>/dev/null; then
    echo "Removed temp project: $tmp"
  else
    echo "Temp dir not empty, left in place: $tmp" >&2
  fi
}

main() {
  local model="${RESUME_MODEL:-BirdNET-2.4}"
  local positionals=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -m|--model)
        [ $# -ge 2 ] || die "$1 needs a value"
        model="$2"
        shift 2
        ;;
      -h|--help)
        usage
        return 0
        ;;
      -*)
        die "unknown option: $1"
        ;;
      *)
        positionals+=("$1")
        shift
        ;;
    esac
  done

  [ "${#positionals[@]}" -ge 2 ] || { usage >&2; exit 1; }
  local action="${positionals[0]}"
  local src="${positionals[1]%/}"
  local tmp="${positionals[2]:-${src}_RESUME}"
  tmp="${tmp%/}"

  assert_known_model "$model"
  MODEL="$model"
  CSV_NAME="detections-${MODEL}.csv"
  echo "Model: $MODEL (complete = $CSV_NAME present)"

  case "$action" in
    split) cmd_split "$src" "$tmp" ;;
    merge) cmd_merge "$src" "$tmp" ;;
    *) die "unknown action: $action (expected split or merge)" ;;
  esac
}

main "$@"
