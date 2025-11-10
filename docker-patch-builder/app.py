import os
import subprocess
import threading
import hashlib
import uuid
import json
import time
import base64
import hmac
from pathlib import Path
import tomllib
from flask import Flask, render_template, request, send_file, jsonify, make_response, Response

app = Flask(__name__)

# Paths and defaults
CONFIG_FILE = 'cc-fw-tools/oc-patches/patch_config'
BUILD_SCRIPT = 'scripts/build_artifact.sh'
DEFAULT_ZIP_ARTIFACT = ''
SESSIONS_DIR = '/app/sessions'
MIRRORS_DIR = '/app/cache/mirrors'
USAGE_DIR = 'usage'
USAGE_LOG = os.path.join(USAGE_DIR, 'usage.log')
USAGE_TXT = os.path.join(USAGE_DIR, 'usage.txt')
USAGE_PATCH_DIR = os.path.join(USAGE_DIR, 'patches')
MAX_CONCURRENT_BUILDS = 3
GC_MAX_AGE_SECS = 20 * 60  # 20 minutes
GC_INTERVAL_SECS = 120

# In-memory build states keyed by session id (pbid)
build_states: dict[str, dict] = {}
# Use a re-entrant lock to avoid deadlocks when helper functions that acquire
# the lock are called from within locked sections.
build_lock = threading.RLock()


def get_state(pbid: str) -> dict:
    with build_lock:
        st = build_states.get(pbid)
        if not st:
            st = {'state': 'idle', 'message': '', 'artifact': '', 'artifact2': '', 'last': time.time()}
            build_states[pbid] = st
        return st


def count_running() -> int:
    with build_lock:
        return sum(1 for s in build_states.values() if s.get('state') == 'running')


def touch_session(pbid: str):
    with build_lock:
        st = get_state(pbid)
        st['last'] = time.time()


def usage_log(event: str, **fields):
    """Write both JSONL and human-readable text usage logs.

    - JSONL: usage/usage.log (machine-friendly)
    - Text:  usage/usage.txt (human-friendly)
    - Large 'patch_config' payload is written to usage/patches/<stamp>-<sess>-<event>.cfg
    """
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        os.makedirs(USAGE_PATCH_DIR, exist_ok=True)
        ts_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())

        # JSON record
        rec = {'ts': ts_iso, 'event': event}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)

        # Prepare text line
        sess = fields.get('session', '')
        sess_short = (sess[:8] + '…') if sess else ''
        repo = fields.get('repo', '')
        # shorten repo URL to owner/repo
        if repo.startswith('http://') or repo.startswith('https://'):
            try:
                repo_short = repo.split('://',1)[1]
            except Exception:
                repo_short = repo
            parts = repo_short.split('/')
            repo_short = '/'.join(parts[-2:]) if len(parts) >= 2 else repo_short
        else:
            repo_short = repo
        branch = fields.get('branch', '')
        fw = fields.get('fw', '')
        adv = fields.get('adv', None)
        artifact = fields.get('artifact', '')
        artifact2 = fields.get('artifact2', '')
        file_ = fields.get('file', '')
        size = fields.get('size', None)
        error = fields.get('error', '')
        code = fields.get('code', None)

        def base(p: str) -> str:
            try:
                return os.path.basename(p)
            except Exception:
                return p

        # Offload big patch_config to a file, and reference it
        cfg_path_txt = ''
        if 'patch_config' in fields and fields['patch_config']:
            try:
                cfg_name = f"{stamp}-{(sess or '')[:8]}-{event}.cfg"
                cfg_full = os.path.join(USAGE_PATCH_DIR, cfg_name)
                with open(cfg_full, 'w', encoding='utf-8') as cf:
                    cf.write(str(fields['patch_config']))
                cfg_path_txt = f"patches/{cfg_name}"
            except Exception:
                cfg_path_txt = '<err>'

        parts_txt = [ts_iso, event]
        if sess_short: parts_txt.append(f"sess={sess_short}")
        if repo_short: parts_txt.append(f"repo={repo_short}")
        if branch:     parts_txt.append(f"branch={branch}")
        if fw:         parts_txt.append(f"fw={fw}")
        if adv is not None: parts_txt.append(f"adv={'yes' if adv else 'no'}")
        if artifact:   parts_txt.append(f"artifact={artifact}")
        if artifact2:  parts_txt.append(f"artifact2={artifact2}")
        if file_:      parts_txt.append(f"file={file_} ({base(file_)})")
        if size is not None and size != '': parts_txt.append(f"size={size}")
        if cfg_path_txt: parts_txt.append(f"cfg={cfg_path_txt}")
        if code is not None: parts_txt.append(f"code={code}")
        if error:      parts_txt.append(f"error={error}")
        text_line = ' | '.join(parts_txt)

        with build_lock:
            with open(USAGE_LOG, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            with open(USAGE_TXT, 'a', encoding='utf-8') as ft:
                ft.write(text_line + '\n')
    except Exception:
        pass


# -------------------------
# Protected usage viewer
# -------------------------
def _get_usage_secret():
    """Load the secret for viewing usage from env or a file path.

    - USAGE_TOKEN: the token/password value
    - USAGE_TOKEN_FILE: path to a file whose contents are the token
    """
    token = os.environ.get('USAGE_TOKEN')
    if not token:
        token_path = os.environ.get('USAGE_TOKEN_FILE')
        if token_path and os.path.isfile(token_path):
            try:
                with open(token_path, 'r', encoding='utf-8', errors='ignore') as f:
                    token = f.read().strip()
            except Exception:
                token = None
    return token


def _basic_ok(req) -> bool:
    """Check HTTP Basic credentials against USAGE_TOKEN.

    Username may be provided via USAGE_USER (defaults to 'admin'). Password must
    match USAGE_TOKEN. Uses constant-time comparison.
    """
    token = _get_usage_secret()
    if not token:
        return False
    user_expected = os.environ.get('USAGE_USER', 'admin')
    hdr = req.headers.get('Authorization', '')
    if not hdr.startswith('Basic '):
        return False
    try:
        raw = base64.b64decode(hdr.split(' ', 1)[1].strip()).decode('utf-8')
        user_supplied, pass_supplied = raw.split(':', 1)
    except Exception:
        return False
    ok_user = hmac.compare_digest(user_supplied, user_expected)
    ok_pass = hmac.compare_digest(pass_supplied, token)
    return ok_user and ok_pass


@app.route('/usage')
def usage_view():
    # Require secret; if not configured, disable endpoint
    if not _get_usage_secret():
        return Response('Usage viewer disabled', status=403, mimetype='text/plain')
    if not _basic_ok(request):
        resp = Response('Authentication required', 401, mimetype='text/plain')
        resp.headers['WWW-Authenticate'] = 'Basic realm="Usage"'
        return resp
    # Choose format
    fmt = request.args.get('format', 'text').lower()
    try:
        lines = int(request.args.get('lines', '200'))
        if lines < 1:
            lines = 200
    except Exception:
        lines = 200
    include_cfg = request.args.get('include_cfg', '1') not in ('0','false','no')

    # For text view, prefer rendering from JSON to allow inline patch_config
    if fmt in ('text','txt') and os.path.isfile(USAGE_LOG):
        try:
            with open(USAGE_LOG, 'r', encoding='utf-8', errors='ignore') as f:
                raw = f.readlines()[-lines:]
            out_lines = []
            for ln in raw:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                ts = rec.get('ts','')
                event = rec.get('event','')
                sess = rec.get('session','')
                sess_short = (sess[:8] + '…') if sess else ''
                repo = rec.get('repo','')
                if repo.startswith('http://') or repo.startswith('https://'):
                    try:
                        rep2 = repo.split('://',1)[1]
                    except Exception:
                        rep2 = repo
                    parts = rep2.split('/')
                    repo_short = '/'.join(parts[-2:]) if len(parts) >= 2 else rep2
                else:
                    repo_short = repo
                branch = rec.get('branch','')
                fw = rec.get('fw','')
                adv = rec.get('adv',None)
                artifact = rec.get('artifact','')
                artifact2 = rec.get('artifact2','')
                file_ = rec.get('file','')
                size = rec.get('size','')
                code = rec.get('code','')
                error = rec.get('error','')
                parts_txt = [ts, event]
                if sess_short: parts_txt.append(f"sess={sess_short}")
                if repo_short: parts_txt.append(f"repo={repo_short}")
                if branch: parts_txt.append(f"branch={branch}")
                if fw: parts_txt.append(f"fw={fw}")
                if adv is not None: parts_txt.append(f"adv={'yes' if adv else 'no'}")
                if artifact: parts_txt.append(f"artifact={artifact}")
                if artifact2: parts_txt.append(f"artifact2={artifact2}")
                if file_: parts_txt.append(f"file={file_}")
                if size not in (None, ''): parts_txt.append(f"size={size}")
                if code not in (None, ''): parts_txt.append(f"code={code}")
                if error: parts_txt.append(f"error={error}")
                out_lines.append(' | '.join(parts_txt))
                if include_cfg and event == 'build_requested':
                    cfg = rec.get('patch_config')
                    if cfg:
                        out_lines.append('---- patch_config ----')
                        out_lines.extend(cfg.splitlines())
                        out_lines.append('----------------------')
            return Response('\n'.join(out_lines) + ('\n' if out_lines else ''), mimetype='text/plain')
        except Exception as e:
            return Response(f'error reading json log: {e}', mimetype='text/plain')

    # Fallback: direct tail of the selected file
    path = USAGE_TXT if fmt in ('text', 'txt') else USAGE_LOG
    if not os.path.isfile(path):
        return Response('No usage yet', mimetype='text/plain')
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            data = f.readlines()
        out = ''.join(data[-lines:])
        return Response(out, mimetype='text/plain' if fmt in ('text','txt') else 'application/json')
    except Exception as e:
        return Response(f'error reading log: {e}', mimetype='text/plain')

# Optional extra fields mapped by patch id
EXTRA_FIELDS = {
    'bowden_length': [
        {'key': 'BOWDEN_LENGTH_MM', 'label': 'Bowden length (mm)', 'type': 'number', 'min': 10, 'max': 999, 'default': '700'},
    ],
    'bed_mesh_temp': [
        {'key': 'BED_MESH_TEMP', 'label': 'Bed mesh temperature (C)', 'type': 'number', 'min': 35, 'max': 99, 'default': '60'},
    ],
}


def load_patches(base_root: str = 'cc-fw-tools'):
    patches = []
    base = Path(base_root) / 'oc-patches'
    if not base.is_dir():
        return patches
    for child in base.iterdir():
        if child.is_dir():
            toml_path = child / 'patch.toml'
            if toml_path.is_file():
                try:
                    with open(toml_path, 'rb') as fp:
                        data = tomllib.load(fp)
                    patches.append({
                        'id': data.get('id'),
                        'name': data.get('name', data.get('id')),
                        'execution_policy': (data.get('execution_policy') or 'MatchesEnv'),
                        'compatible_versions': data.get('compatible_versions', []),
                    })
                except Exception:
                    continue
    patches.sort(key=lambda p: (0 if p['id'] == 'base' else 1, p['name'] or ''))
    return patches


# Upstream repos and utilities for selection
REPOS = [
    { 'key': 'harpua', 'label': 'harpua555/cc-fw-tools', 'url': 'https://github.com/harpua555/cc-fw-tools' },
    { 'key': 'oc',     'label': 'OpenCentauri/cc-fw-tools', 'url': 'https://github.com/OpenCentauri/cc-fw-tools' },
]


def list_remote_branches(repo_url: str, filter_key: str) -> list[str]:
    try:
        res = subprocess.run(['git', 'ls-remote', '--heads', repo_url], capture_output=True, text=True, check=True)
        names = []
        for ln in res.stdout.splitlines():
            if '\trefs/heads/' in ln:
                names.append(ln.split('\trefs/heads/', 1)[1].strip())
        if filter_key == 'harpua':
            names = [b for b in names if b == 'main' or b.startswith('feature/')]
        names = sorted(names, key=lambda b: (0 if b == 'main' else 1, b))
        return names or ['main']
    except Exception:
        return ['main']


def _slug_repo(repo_url: str) -> str:
    s = repo_url.replace('https://', '').replace('http://', '').strip('/')
    return s.replace('/', '_')


def sync_cc_fw_tools_session(repo_url: str, branch: str, session_root: str):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(MIRRORS_DIR, exist_ok=True)
    mirror = os.path.join(MIRRORS_DIR, _slug_repo(repo_url) + '.git')
    # Mirror clone or update
    if os.path.isdir(mirror):
        subprocess.run(['git', '-C', mirror, 'remote', 'set-url', '--push', 'origin', repo_url], check=False)
        subprocess.run(['git', '-C', mirror, 'fetch', '--all', '--tags', '--prune'], check=True)
    else:
        subprocess.run(['git', 'clone', '--mirror', repo_url, mirror], check=True)
    # Per-session checkout
    repo_path = os.path.join(session_root, 'cc-fw-tools')
    if os.path.isdir(repo_path):
        # reset existing session checkout
        subprocess.run(['git', '-C', repo_path, 'remote', 'set-url', 'origin', repo_url], check=False)
        subprocess.run(['git', '-C', repo_path, 'fetch', '--tags', '--force', '--prune', 'origin'], check=False)
        subprocess.run(['git', '-C', repo_path, 'fetch', '--force', 'origin', branch], check=False)
        subprocess.run(['git', '-C', repo_path, 'checkout', '-B', branch, f'origin/{branch}'], check=False)
        subprocess.run(['git', '-C', repo_path, 'reset', '--hard', f'origin/{branch}'], check=False)
    else:
        os.makedirs(session_root, exist_ok=True)
        subprocess.run(['git', 'clone', '--shared', '--no-checkout', mirror, repo_path], check=True)
        # Point origin to the real remote (for LFS), keep a 'cache' remote to the mirror for fast fetches
        subprocess.run(['git', '-C', repo_path, 'remote', 'set-url', 'origin', repo_url], check=False)
        subprocess.run(['git', '-C', repo_path, 'remote', 'add', 'cache', mirror], check=False)
        subprocess.run(['git', '-C', repo_path, 'fetch', 'cache', '--tags', '--force'], check=False)
        subprocess.run(['git', '-C', repo_path, 'fetch', 'cache', branch], check=False)
        subprocess.run(['git', '-C', repo_path, 'checkout', '-B', branch, f'cache/{branch}'], check=True)
    # LFS for session checkout
    subprocess.run(['git', 'lfs', 'install'], check=False)
    subprocess.run(['git', '-C', repo_path, 'lfs', 'pull'], check=False)


def current_source(base_repo: str = 'cc-fw-tools'):
    try:
        if os.path.isdir(os.path.join(base_repo, '.git')):
            r1 = subprocess.run(['git', '-C', base_repo, 'remote', 'get-url', 'origin'], capture_output=True, text=True, check=True)
            origin = (r1.stdout or '').strip()
            r2 = subprocess.run(['git', '-C', base_repo, 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True, check=True)
            branch = (r2.stdout or '').strip()
            return origin, branch
    except Exception:
        pass
    return None, None


def _ensure_pbid():
    pbid = request.cookies.get('pbid')
    if not pbid:
        pbid = uuid.uuid4().hex
    touch_session(pbid)
    return pbid


@app.route('/', methods=['GET', 'POST'])
def index():
    pbid = _ensure_pbid()
    session_root = os.path.join(SESSIONS_DIR, pbid)
    session_repo = os.path.join(session_root, 'cc-fw-tools')
    errors = []
    form_values = {}
    patches = []

    # Repo/branch lists for UI
    branches_map = { r['key']: list_remote_branches(r['url'], r['key']) for r in REPOS }
    repo_selected = REPOS[0]['url']
    branch_selected = branches_map[REPOS[0]['key']][0] if branches_map[REPOS[0]['key']] else 'main'
    
    # Determine initialization state
    need_init = not os.path.isdir(os.path.join(session_repo, '.git'))
    if not need_init:
        # If already initialized, prefer actual current origin/branch for UI
        cur_repo, cur_branch = current_source(session_repo)
        if cur_repo:
            repo_selected = cur_repo
        if cur_branch:
            branch_selected = cur_branch
        patches = load_patches(session_repo)

    # Reflect background build state on GET renders
    download_ready = False
    artifact_name = ''
    error_text = None
    st = get_state(pbid)
    with build_lock:
        artifact2 = ''
        if st['state'] == 'success':
            artifact_name = st.get('artifact') or ''
            artifact2 = st.get('artifact2') or ''
            download_ready = True
        elif st['state'] == 'error':
            error_text = f"Build failed: {st['message']}"

    if request.method == 'POST':
        # 1) Inputs
        fw_version = request.form.get('fw_version', os.environ.get('FW_VER', '1.1.40'))
        form_values['fw_version'] = fw_version
        repo_selected = request.form.get('repo_url', repo_selected)
        branch_selected = request.form.get('branch_name', branch_selected)
        form_values['repo_url'] = repo_selected
        form_values['branch_name'] = branch_selected
        action = request.form.get('action', 'build')

        if action == 'init':
            try:
                sync_cc_fw_tools_session(repo_selected, branch_selected, session_root)
                patches = load_patches(session_repo)
                need_init = False
                resp = make_response(render_template('index.html', patches=patches, extra_fields=EXTRA_FIELDS, download_ready=False, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init))
                resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
                return resp
            except Exception as e:
                error_msg = f"Source initialization failed: {str(e)}"
                resp = make_response(render_template('index.html', error=error_msg, patches=[], extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=True))
                resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
                return resp

        # 2) Build path: ensure upstream synchronized first
        try:
            # Concurrency guard
            if count_running() >= MAX_CONCURRENT_BUILDS:
                return render_template('index.html', error=f"Server is busy (max {MAX_CONCURRENT_BUILDS} concurrent builds). Please try again shortly.", patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)

            sync_cc_fw_tools_session(repo_selected, branch_selected, session_root)
            patches = load_patches(session_repo)
            need_init = False

            # Merge patch_config (preserve upstream defaults) with replacement
            config_path = os.path.join(session_repo, 'oc-patches', 'patch_config')
            base_content = ''
            has_bootstrap_md5 = False
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r', encoding='utf-8', errors='ignore') as bcf:
                        base_content = bcf.read()
                    for ln in base_content.splitlines():
                        if ln.strip().startswith('OC_BOOTSTRAP_MD5='):
                            has_bootstrap_md5 = True
                            break
                except Exception:
                    pass

            override_lines = [
                '# Generated overrides by patch-builder UI',
                f'# FW_VER={fw_version}',
            ]

            if not has_bootstrap_md5:
                bootstrap = Path(session_repo) / 'oc-patches/base-patch/OpenCentauri-bootstrap.tar.gz'
                if bootstrap.is_file():
                    md5 = hashlib.md5()
                    with open(bootstrap, 'rb') as bf:
                        for chunk in iter(lambda: bf.read(8192), b''):
                            md5.update(chunk)
                    override_lines.append(f"OC_BOOTSTRAP_MD5={md5.hexdigest()}")

            # Toggle MatchesEnv patches to explicit true/false (default true)
            keys_to_override = set()
            overrides_map = {}
            for p in patches:
                policy = (p.get('execution_policy') or '').lower()
                pid = p.get('id') or ''
                if policy == 'matchesenv':
                    enabled_val = (request.form.get(f'enable__{pid}') or 'true').lower()
                    form_values[f'enable__{pid}'] = enabled_val
                    k = pid.upper()
                    keys_to_override.add(k)
                    v = 'true' if enabled_val=='true' else 'false'
                    override_lines.append(f"{k}={v}")
                    overrides_map[k] = v

                # Extra optional inputs
                for field in EXTRA_FIELDS.get(pid, []):
                    form_key = f"{pid}__{field['key']}"
                    val = (request.form.get(form_key, '') or '').strip()
                    form_values[form_key] = val
                    if field.get('type') == 'number' and val != '':
                        if not val.isdigit():
                            errors.append(f"{field['label']} must be an integer.")
                        else:
                            num = int(val, 10)
                            if 'min' in field and num < int(field['min']):
                                errors.append(f"{field['label']} must be >= {field['min']}.")
                            if 'max' in field and num > int(field['max']):
                                errors.append(f"{field['label']} must be <= {field['max']}.")
                    if val:
                        k2 = field['key']
                        keys_to_override.add(k2)
                        override_lines.append(f"{k2}={val}")
                        overrides_map[k2] = val

            if errors:
                return render_template('index.html', errors=errors, patches=patches, extra_fields=EXTRA_FIELDS, download_ready=False, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)

            # Compose final config: remove any existing keys we override, then append overrides
            final_lines = []
            replaced_keys = set()
            if base_content:
                for ln in base_content.splitlines():
                    s = ln.strip()
                    if not s or s.startswith('#'):
                        # If commented default like '#KEY=value' exists and we override KEY, replace inline
                        if s.startswith('#') and '=' in s:
                            raw = s[1:].strip()
                            key = raw.split('=',1)[0].strip()
                            if key in overrides_map:
                                final_lines.append(f"{key}={overrides_map[key]}")
                                replaced_keys.add(key)
                                continue
                        final_lines.append(ln)
                        continue
                    # Match KEY=value lines
                    if '=' in ln:
                        k = ln.split('=', 1)[0].strip()
                        if k in keys_to_override:
                            # Drop old entry; will be replaced by override later unless replaced inline above
                            continue
                    final_lines.append(ln)
                # Ensure a blank line before overrides for readability
                if final_lines and final_lines[-1].strip() != '':
                    final_lines.append('')
            # Append overrides only for keys not already replaced inline
            for ol in override_lines:
                k = ol.split('=',1)[0]
                if k in replaced_keys:
                    continue
                final_lines.append(ol)
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(final_lines).rstrip() + "\n")

            # 3) Kick off build in the background
            adv_with_app = (request.form.get('adv_app_binary') == 'on')

            # Prepare a per-session verbose build log under artifacts/<pbid>/build.log
            log_dir = os.path.join('artifacts', pbid)
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, 'build.log')
            try:
                with open(log_path, 'w', encoding='utf-8') as lf:
                    lf.write('==== OpenCentauri Patch Builder Verbose Log ====' + "\n")
                    lf.write(f"Repo: {repo_selected}\nBranch: {branch_selected}\nFW: {fw_version}\nSession: {pbid}\n\n")
                    lf.write(f"== patch_config ({config_path}) ==\n")
                    try:
                        with open(config_path, 'r', encoding='utf-8', errors='ignore') as cf:
                            lf.write(cf.read())
                    except Exception as e:
                        lf.write(f"<error reading patch_config: {e}>\n")
                    lf.write('\n==== Begin build.sh output ====\n')
            except Exception:
                pass
            # Record usage of the requested build with a snapshot of the config
            try:
                with open(config_path, 'r', encoding='utf-8', errors='ignore') as cf:
                    cfg_snapshot = cf.read()
            except Exception:
                cfg_snapshot = ''
            usage_log('build_requested', session=pbid, repo=repo_selected, branch=branch_selected, fw=fw_version, adv=bool(adv_with_app), patch_config=cfg_snapshot)

            def _runner(version: str, with_app: bool):
                try:
                    args = ['/bin/bash', BUILD_SCRIPT, version]
                    if with_app:
                        args.append('1')
                    env = os.environ.copy()
                    env['SESSION_DIR'] = session_root
                    env['ARTIFACTS_DIR'] = os.path.join('artifacts', pbid)
                    os.makedirs(env['ARTIFACTS_DIR'], exist_ok=True)
                    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                    milestones = []
                    artifact_path = ''
                    app_path = ''
                    # Open log file for append
                    lf = None
                    try:
                        lf = open(os.path.join(env['ARTIFACTS_DIR'], 'build.log'), 'a', encoding='utf-8')
                    except Exception:
                        lf = None
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            if proc.poll() is not None:
                                break
                            continue
                        L = line.strip()
                        if lf:
                            try:
                                lf.write(L + "\n")
                                lf.flush()
                            except Exception:
                                pass
                        # Friendly status milestones
                        if 'Downloading' in L and 'firmware' in L:
                            if 'Downloading firmware...' not in milestones:
                                milestones.append('Downloading firmware...')
                        elif 'Hash OK' in L:
                            if 'Hash confirmed' not in milestones:
                                milestones.append('Hash confirmed')
                        elif 'Unpacking the firmware' in L:
                            if 'Unpacking firmware...' not in milestones:
                                milestones.append('Unpacking firmware...')
                        elif 'Patching the firmware' in L or 'Applying patch' in L:
                            if 'Applying patches...' not in milestones:
                                milestones.append('Applying patches...')
                        elif 'Re-packing the firmware' in L:
                            if 'Repacking firmware...' not in milestones:
                                milestones.append('Repacking firmware...')
                        elif 'Packing done' in L:
                            if 'Packaging complete' not in milestones:
                                milestones.append('Packaging complete')
                        if 'Artifact ready at ' in L:
                            artifact_path = L.split('Artifact ready at ',1)[1].strip()
                        if 'Patched app exported to ' in L:
                            app_path = L.split('Patched app exported to ',1)[1].strip()
                        with build_lock:
                            st = get_state(pbid)
                            st['message'] = '\n'.join(milestones[-6:]) if milestones else (st.get('message') or 'Starting...')
                    rescode = proc.wait()
                    if lf:
                        try:
                            lf.write(f"==== build.sh exited with code {rescode} ====\n")
                            lf.flush()
                            lf.close()
                        except Exception:
                            pass
                    if rescode == 0:
                        with build_lock:
                            st = get_state(pbid)
                            st['state'] = 'success'
                            st['artifact'] = artifact_path or DEFAULT_ZIP_ARTIFACT
                            st['artifact2'] = app_path if with_app and app_path else ''
                    else:
                        with build_lock:
                            st = get_state(pbid)
                            st['state'] = 'error'
                            st['message'] = f"Build failed with code {rescode}"
                            st['artifact'] = ''
                            st['artifact2'] = ''
                except Exception as ex:
                    with build_lock:
                        st = get_state(pbid)
                        st['state'] = 'error'
                        st['message'] = str(ex)
                        st['artifact'] = ''
                        st['artifact2'] = ''

            with build_lock:
                stx = get_state(pbid)
                if stx['state'] == 'running':
                    return render_template('index.html', error='A build is already in progress for your session. Please wait.', patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
                stx['state'] = 'running'
                stx['message'] = 'Starting...'

            threading.Thread(target=_runner, args=(fw_version, adv_with_app), daemon=True).start()

            resp = make_response(render_template('index.html', building=True, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=False))
            resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
            return resp

        except subprocess.CalledProcessError as e:
            error_msg = f"Build Failed! Error: {e.stderr}"
            return render_template('index.html', error=error_msg, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
        except Exception as e:
            error_msg = f"An unexpected error occurred: {str(e)}"
            return render_template('index.html', error=error_msg, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)

    # GET
    if error_text:
        resp = make_response(render_template('index.html', error=error_text, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init))
        resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
        return resp
    if download_ready:
        resp = make_response(render_template('index.html', download_ready=True, artifact_name=artifact_name, artifact2_name=artifact2, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=False))
        resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
        return resp
    resp = make_response(render_template('index.html', download_ready=False, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init))
    resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
    return resp


@app.route('/download/<path:filename>')
def download(filename):
    pbid = request.cookies.get('pbid', '')
    artifacts_root = os.path.abspath(os.path.join('artifacts', pbid))
    target_path = os.path.abspath(os.path.join('.', filename))
    if not artifacts_root or not target_path.startswith(artifacts_root) or not os.path.isfile(target_path):
        return ("Not Found", 404)

    # Log and send download; add no-cache headers to avoid browser cache confusion
    try:
        size = os.path.getsize(target_path)
    except Exception:
        size = -1
    usage_log('download_start', session=pbid, file=filename, size=size)
    resp = send_file(target_path, as_attachment=True)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'

    def _cleanup():
        try:
            os.remove(target_path)
        except Exception:
            pass
        try:
            with build_lock:
                st = get_state(pbid)
                if st.get('state') == 'success':
                    st['state'] = 'idle'
                    st['message'] = ''
                    st['artifact'] = ''
                    st['artifact2'] = ''
                    st['last'] = time.time()
            usage_log('download_complete', session=pbid, file=filename)
        except Exception:
            pass

    resp.call_on_close(_cleanup)
    return resp


@app.route('/status')
def status():
    pbid = request.cookies.get('pbid', '')
    with build_lock:
        st = get_state(pbid) if pbid else {'state': 'idle', 'message': ''}
        return jsonify({
            'state': st.get('state', 'idle'),
            'message': st.get('message', ''),
            'artifact': st.get('artifact') or '',
            'artifact2': st.get('artifact2') or '',
        })


@app.route('/log')
def view_log():
    pbid = request.cookies.get('pbid', '')
    if not pbid:
        return Response('no session', mimetype='text/plain')
    log_path = os.path.abspath(os.path.join('artifacts', pbid, 'build.log'))
    if not os.path.isfile(log_path):
        return Response('log not found', mimetype='text/plain')
    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            data = f.read()
        return Response(data, mimetype='text/plain')
    except Exception as e:
        return Response(f'error reading log: {e}', mimetype='text/plain')


@app.route('/init', methods=['POST'])
def init_source():
    pbid = _ensure_pbid()
    session_root = os.path.join(SESSIONS_DIR, pbid)
    repo_selected = request.form.get('repo_url', REPOS[0]['url'])
    branch_selected = request.form.get('branch_name', 'main')
    # prevent overlap with builds
    with build_lock:
        st = get_state(pbid)
        if st.get('state') == 'running':
            return jsonify({'ok': False, 'error': 'A build is currently running. Please wait.'}), 409
        st['state'] = 'init_running'
        st['message'] = f'Loading source from {repo_selected} ({branch_selected})...'
        st['last'] = time.time()
    usage_log('init_request', session=pbid, repo=repo_selected, branch=branch_selected)

    def _init_worker(repo_url: str, branch: str, sess_root: str):
        try:
            with build_lock:
                st = get_state(pbid)
                st['message'] = f'Cloning/fetching {branch}...'
                st['last'] = time.time()
            sync_cc_fw_tools_session(repo_url, branch, sess_root)
            with build_lock:
                st = get_state(pbid)
                st['state'] = 'init_success'
                st['message'] = 'Source loaded. Patches available.'
                st['last'] = time.time()
            usage_log('init_success', session=pbid, repo=repo_url, branch=branch)
        except Exception as ex:
            with build_lock:
                st = get_state(pbid)
                st['state'] = 'error'
                st['message'] = f'Init failed: {ex}'
                st['last'] = time.time()
            usage_log('init_error', session=pbid, repo=repo_url, branch=branch, error=str(ex))

    threading.Thread(target=_init_worker, args=(repo_selected, branch_selected, session_root), daemon=True).start()
    resp = make_response(jsonify({'ok': True}))
    resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
    return resp


# Fallback GET-based init to avoid any client/FormData quirks
@app.route('/init-get', methods=['GET'])
def init_source_get():
    pbid = _ensure_pbid()
    session_root = os.path.join(SESSIONS_DIR, pbid)
    repo_selected = request.args.get('repo_url', REPOS[0]['url'])
    branch_selected = request.args.get('branch_name', 'main')
    with build_lock:
        st = get_state(pbid)
        if st.get('state') == 'running':
            return jsonify({'ok': False, 'error': 'A build is currently running. Please wait.'}), 409
        st['state'] = 'init_running'
        st['message'] = f'Loading source from {repo_selected} ({branch_selected})...'

    def _init_worker(repo_url: str, branch: str, sess_root: str):
        try:
            with build_lock:
                st = get_state(pbid)
                st['message'] = f'Cloning/fetching {branch}...'
                st['last'] = time.time()
            sync_cc_fw_tools_session(repo_url, branch, sess_root)
            with build_lock:
                st = get_state(pbid)
                st['state'] = 'init_success'
                st['message'] = 'Source loaded. Patches available.'
                st['last'] = time.time()
            usage_log('init_success', session=pbid, repo=repo_url, branch=branch)
        except Exception as ex:
            with build_lock:
                st = get_state(pbid)
                st['state'] = 'error'
                st['message'] = f'Init failed: {ex}'
                st['last'] = time.time()
            usage_log('init_error', session=pbid, repo=repo_url, branch=branch, error=str(ex))

    threading.Thread(target=_init_worker, args=(repo_selected, branch_selected, session_root), daemon=True).start()
    resp = make_response(jsonify({'ok': True}))
    resp.set_cookie('pbid', pbid, httponly=True, samesite='Lax')
    return resp


@app.route('/reset', methods=['POST'])
def reset_source():
    # Prevent reset while a build is running
    pbid = request.cookies.get('pbid', '')
    with build_lock:
        st = get_state(pbid)
        if st.get('state') in ('running', 'init_running'):
            return jsonify({'ok': False, 'error': 'An operation is running. Please wait.'}), 409
        # Reset state
        st['state'] = 'idle'
        st['message'] = ''
        st['artifact'] = ''
        st['artifact2'] = ''
    try:
        # Remove the repo directory entirely to ensure a clean re-init next time
        import shutil
        session_root = os.path.join(SESSIONS_DIR, pbid) if pbid else None
        if session_root and os.path.isdir(session_root):
            shutil.rmtree(session_root, ignore_errors=True)
        # Recreate empty session dir
        if session_root:
            os.makedirs(session_root, exist_ok=True)
        return jsonify({'ok': True})
    except Exception as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
else:
    # Start a simple GC thread when running under Gunicorn
    _gc_started = globals().get('_gc_started', False)
    def _gc_loop():
        while True:
            try:
                now = time.time()
                # Build candidate list from dirs
                pbids = set()
                try:
                    for d in os.listdir(SESSIONS_DIR):
                        pbids.add(d)
                except Exception:
                    pass
                try:
                    for d in os.listdir('artifacts'):
                        pbids.add(d)
                except Exception:
                    pass
                for pbid in pbids:
                    st = get_state(pbid)
                    # Skip active sessions
                    if st.get('state') in ('running', 'init_running'):
                        continue
                    last = st.get('last') or now
                    if now - last < GC_MAX_AGE_SECS:
                        continue
                    # Remove session repo and artifacts
                    try:
                        import shutil
                        sess_root = os.path.join(SESSIONS_DIR, pbid)
                        art_root = os.path.join('artifacts', pbid)
                        if os.path.isdir(sess_root):
                            shutil.rmtree(sess_root, ignore_errors=True)
                        if os.path.isdir(art_root):
                            shutil.rmtree(art_root, ignore_errors=True)
                        usage_log('gc_cleanup', session=pbid)
                        with build_lock:
                            st['state'] = 'idle'
                            st['message'] = ''
                            st['artifact'] = ''
                            st['artifact2'] = ''
                            st['last'] = now
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(GC_INTERVAL_SECS)

    if not _gc_started:
        try:
            t = threading.Thread(target=_gc_loop, daemon=True)
            t.start()
            globals()['_gc_started'] = True
        except Exception:
            pass
