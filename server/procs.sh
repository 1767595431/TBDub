# 只匹配真正的 python -m 进程，避免 pgrep -f 误杀带同样字样的 shell。
py_comm_ok() {
  case "$(cat "/proc/$1/comm" 2>/dev/null || true)" in
    python*|pt_*) return 0 ;;
    *) return 1 ;;
  esac
}

py_module_pids() {
  local module="$1" pid cmd
  for pid in /proc/[0-9]*; do
    pid="${pid#/proc/}"
    py_comm_ok "$pid" || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *"-m ${module} "*|*" -m ${module}"|*" -m ${module} "*) printf '%s\n' "$pid" ;;
    esac
  done
}

inference_pids() {
  local pid cmd
  for pid in /proc/[0-9]*; do
    pid="${pid#/proc/}"
    py_comm_ok "$pid" || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *inference.py*) printf '%s\n' "$pid" ;;
    esac
  done
}

api_pids() {
  local pid cmd
  for pid in /proc/[0-9]*; do
    pid="${pid#/proc/}"
    py_comm_ok "$pid" || continue
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *server.worker*) continue ;;
    esac
    case "$cmd" in
      *"-m server "*|*" -m server"|*" -m server "*) printf '%s\n' "$pid" ;;
    esac
  done
}
