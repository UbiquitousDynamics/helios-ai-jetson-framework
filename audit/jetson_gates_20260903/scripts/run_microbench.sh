#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 AUDIT_DIR DEPLOYMENT_PYTHON" >&2
    exit 2
fi

audit_dir="$1"
python_bin="$2"
repo="$audit_dir/repo"
artifacts="$audit_dir/artifacts"
script="$audit_dir/scripts/jetson_microbench.py"
export PYTHONPATH="$audit_dir/python-packages"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

exec 3>"$artifacts/microbench_runner.log"
printf 'formal_start=%s\n' "$(date --iso-8601=seconds)" >&3
printf 'command=taskset -c 3 %q %q --repo %q --cpu 3 --repeats 5 --scale 1 --blas-threads 1\n' \
    "$python_bin" "$script" "$repo" >&3
sha256sum "$script" "$repo/embeddings.npz" >&3
git -C "$repo" status --short --branch >&3
git -C "$repo" rev-parse HEAD >&3
sudo -n nvpmodel -q >&3 2>&1
sudo -n jetson_clocks --show >&3 2>&1

tegrastats_pid=""
sudo -n tegrastats --interval 1000 --logfile "$artifacts/tegrastats_microbench.log" &
tegrastats_pid="$!"
sleep 1
if kill -0 "$tegrastats_pid" 2>/dev/null; then
    printf 'tegrastats_pid=%s\n' "$tegrastats_pid" >&3
else
    tegrastats_pid=""
    printf 'tegrastats_start_failed=true\n' >&3
fi

cleanup() {
    if [ -n "$tegrastats_pid" ]; then
        sudo -n kill "$tegrastats_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT

for run in 1 2 3; do
    message="run_${run}_start=$(date --iso-8601=seconds)"
    printf '%s\n' "$message"
    printf '%s\n' "$message" >&3
    taskset -c 3 "$python_bin" "$script" \
        --repo "$repo" \
        --cpu 3 \
        --repeats 5 \
        --scale 1 \
        --blas-threads 1 \
        > "$artifacts/microbench_run_${run}.ndjson" \
        2> "$artifacts/microbench_run_${run}.stderr"
    message="run_${run}_exit=0 end=$(date --iso-8601=seconds)"
    printf '%s\n' "$message"
    printf '%s\n' "$message" >&3
    sleep 10
done

printf 'formal_end=%s\n' "$(date --iso-8601=seconds)" >&3
