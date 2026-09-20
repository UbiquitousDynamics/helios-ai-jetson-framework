#!/usr/bin/env bash
set -u

section() {
    printf '\n===== %s =====\n' "$1"
}

run() {
    printf '$ %s\n' "$*"
    "$@" 2>&1 || printf '[exit=%s]\n' "$?"
}

show_file() {
    local path="$1"
    printf '%s:\n' "$path"
    if [ -r "$path" ]; then
        sed -n '1,240p' "$path"
    else
        printf '[unavailable]\n'
    fi
}

section identity
run date --iso-8601=seconds
run hostname
run id
run uname -a
run git -C "${AUDIT_REPO:?AUDIT_REPO is required}" status --short --branch
run git -C "$AUDIT_REPO" rev-parse HEAD
run git -C "$AUDIT_REPO" symbolic-ref --short HEAD

section jetson_release
show_file /proc/device-tree/model
show_file /proc/device-tree/compatible
show_file /etc/nv_tegra_release
show_file /etc/os-release
run dpkg-query -W nvidia-jetpack nvidia-l4t-core nvidia-l4t-kernel nvidia-l4t-cuda
run apt-cache policy nvidia-jetpack
run sudo -n nvpmodel -q --verbose
run sudo -n jetson_clocks --show

section runtime
run bash -lc 'command -v python; python --version; command -v python3; python3 --version; command -v python3.10; python3.10 --version; command -v pip; pip --version; command -v pip3; pip3 --version'
run git --version

section cpu
run lscpu
show_file /sys/devices/system/cpu/online
show_file /sys/devices/system/cpu/offline
for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
    [ -d "$cpu_dir/cpufreq" ] || continue
    cpu_name="${cpu_dir##*/}"
    printf '%s governor=' "$cpu_name"
    tr -d '\n' < "$cpu_dir/cpufreq/scaling_governor" 2>/dev/null || true
    printf ' min_khz='
    tr -d '\n' < "$cpu_dir/cpufreq/scaling_min_freq" 2>/dev/null || true
    printf ' max_khz='
    tr -d '\n' < "$cpu_dir/cpufreq/scaling_max_freq" 2>/dev/null || true
    printf ' current_khz='
    tr -d '\n' < "$cpu_dir/cpufreq/scaling_cur_freq" 2>/dev/null || true
    printf '\n'
done
run bash -lc 'command -v taskset; taskset -pc $$'

section thermal
for zone in /sys/class/thermal/thermal_zone*; do
    [ -d "$zone" ] || continue
    printf '%s type=' "${zone##*/}"
    tr -d '\n' < "$zone/type" 2>/dev/null || true
    printf ' temp_mC='
    tr -d '\n' < "$zone/temp" 2>/dev/null || true
    printf '\n'
done
run bash -lc 'command -v tegrastats && sudo -n tegrastats --interval 1000 --count 3'

section audio_processes_and_server
run ps -ef
run bash -lc 'command -v pactl; pactl info; pactl list short cards; pactl list short sources; pactl list short sinks; pactl list short modules'
run bash -lc 'command -v pacmd; pacmd info; pacmd list-cards; pacmd list-sources; pacmd list-sinks; pacmd list-modules'
run bash -lc 'command -v pw-cli; pw-cli info all'
run bash -lc 'command -v wpctl; wpctl status'
run systemctl --user --no-pager --full status pipewire pipewire-pulse pulseaudio

section alsa
run bash -lc 'command -v arecord; arecord --version; arecord -l; arecord -L'
run bash -lc 'command -v aplay; aplay --version; aplay -l; aplay -L'
show_file /proc/asound/cards
show_file /proc/asound/pcm
show_file /proc/asound/version
show_file /etc/asound.conf
show_file "$HOME/.asoundrc"
run lsusb
run bash -lc 'for card in /sys/class/sound/card*; do echo "--- $card"; udevadm info --query=property --path="$(readlink -f "$card")"; done'

section audio_configuration
run bash -lc 'find /etc/pulse "$HOME/.config/pulse" /etc/pipewire "$HOME/.config/pipewire" -maxdepth 3 -type f -print 2>/dev/null'
for config in /etc/pulse/client.conf /etc/pulse/daemon.conf /etc/pulse/default.pa "$HOME/.config/pulse/client.conf" "$HOME/.config/pulse/daemon.conf" "$HOME/.config/pulse/default.pa" /etc/pipewire/pipewire.conf /etc/pipewire/pipewire-pulse.conf; do
    [ -e "$config" ] && show_file "$config"
done

section relevant_packages
run dpkg-query -W 'pulseaudio*' 'pipewire*' 'libwebrtc-audio-processing*' 'speex*' 'portaudio*' 'python3*'
run bash -lc 'python3 -m pip list --format=freeze 2>/dev/null | sort'

section storage_and_memory
run free -h
run df -h "$AUDIT_REPO"

section end
run date --iso-8601=seconds
