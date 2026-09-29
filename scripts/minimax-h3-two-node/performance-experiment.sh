# Opt-in, stable-Ref2VA-only experiment. Absent marker keeps the native path.
h3_performance_experiment_args() {
  H3_PERFORMANCE_MOUNTS=()
  H3_PERFORMANCE_PROFILE_ARGS=()
  [[ $H3_SELECTED_PROFILE == heretic-ref2va-v1 ]] || return 0
  local directory=/etc/minimax-h3/performance-experiment-20260914
  if [[ ${H3_TESTING:-0} == 1 && -n ${H3_TEST_PERFORMANCE_DIR:-} ]]; then
    directory=$H3_TEST_PERFORMANCE_DIR
  fi
  [[ -e "$directory/enabled" || -L "$directory/enabled" ]] || return 0
  python3 - "$directory" "${H3_TESTING:-0}" <<'PY' || return $?
import hashlib,pathlib,stat,sys
root=pathlib.Path(sys.argv[1]); testing=sys.argv[2]=='1'
expected='3d957055aa2520cc9985c51ddaff58408cbcac6a11e511261b2128c8287fa7b4'
assert root.is_dir() and not root.is_symlink(), 'unsafe experiment directory'
for p in (root,root/'enabled',root/'cache_context.py'):
 assert not p.is_symlink(), 'unsafe experiment path'
 if not testing:
  st=p.stat(); assert st.st_uid==0 and not (stat.S_IMODE(st.st_mode)&0o022), 'experiment must be root-owned and not writable by other users'
assert (root/'enabled').read_text().strip()=='cache-c3-r010', 'unknown H3 performance experiment'
assert hashlib.sha256((root/'cache_context.py').read_bytes()).hexdigest()==expected, 'cache streak patch hash mismatch'
PY
  H3_PERFORMANCE_MOUNTS=(-v "$directory/cache_context.py:/usr/local/lib/python3.12/dist-packages/cache_dit/caching/cache_contexts/cache_context.py:ro")
  H3_PERFORMANCE_PROFILE_ARGS=(--cache-backend cache_dit --cache-config
    '{"Fn_compute_blocks":1,"Bn_compute_blocks":0,"max_warmup_steps":4,"max_cached_steps":3,"residual_diff_threshold":0.10,"max_continuous_cached_steps":1,"enable_taylorseer":false}'
    --enable-cache-dit-summary)
}
