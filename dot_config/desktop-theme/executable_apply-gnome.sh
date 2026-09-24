#!/bin/sh
# Apply or restore the GNOME-facing part of the Rich Black theme.
set -eu

schema=org.gnome.desktop.interface
state=${XDG_CONFIG_HOME:-"$HOME/.config"}/desktop-theme/gnome-gsettings.previous

keys='color-scheme gtk-theme icon-theme cursor-theme font-name monospace-font-name'

supports_key() {
    gsettings range "$schema" "$1" >/dev/null 2>&1
}

writable_key() {
    [ "$(gsettings writable "$schema" "$1" 2>/dev/null || true)" = true ]
}

save_previous() {
    [ -e "$state" ] && return

    state_tmp=$(mktemp "${state}.tmp.XXXXXX")
    trap 'rm -f "$state_tmp"' EXIT HUP INT TERM
    {
        printf '%s\n' '# Values captured before Rich Black was applied.'
        for key in $keys; do
            if supports_key "$key"; then
                printf '%s\t%s\n' "$key" "$(gsettings get "$schema" "$key")"
            fi
        done
    } >"$state_tmp"
    mv "$state_tmp" "$state"
    trap - EXIT HUP INT TERM
}

set_value() {
    key=$1
    value=$2
    if supports_key "$key" && writable_key "$key"; then
        gsettings set "$schema" "$key" "$value"
        printf 'set %s = %s\n' "$key" "$value"
    else
        printf 'skip %s (schema key unavailable or not writable)\n' "$key" >&2
    fi
}

apply_theme() {
    save_previous
    set_value color-scheme prefer-dark
    set_value gtk-theme Arc-Darker
    set_value icon-theme breeze-dark
    set_value cursor-theme default
    set_value font-name 'Noto Sans 10'
    set_value monospace-font-name 'BlexMono Nerd Font 11'
    printf 'Previous values: %s\n' "$state"
}

restore_theme() {
    [ -f "$state" ] || {
        printf 'No saved GNOME values at %s\n' "$state" >&2
        exit 1
    }

    while IFS="$(printf '\t')" read -r key value; do
        case $key in
            ''|'#'*) continue ;;
        esac
        if supports_key "$key" && writable_key "$key"; then
            gsettings set "$schema" "$key" "$value"
            printf 'restored %s = %s\n' "$key" "$value"
        else
            printf 'skip %s (schema key unavailable or not writable)\n' "$key" >&2
        fi
    done <"$state"
    mv "$state" "${state}.restored"
    printf 'Saved values moved to %s\n' "${state}.restored"
}

case ${1:---apply} in
    --apply) apply_theme ;;
    --restore) restore_theme ;;
    --help|-h)
        printf 'Usage: %s [--apply|--restore]\n' "$0"
        printf 'Apply Rich Black, or restore the values captured before its first apply.\n'
        ;;
    *)
        printf 'Usage: %s [--apply|--restore]\n' "$0" >&2
        exit 2
        ;;
esac
