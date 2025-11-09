import os
import subprocess
import threading
import hashlib
from pathlib import Path
import tomllib
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# Paths and defaults
CONFIG_FILE = 'cc-fw-tools/oc-patches/patch_config'
BUILD_SCRIPT = 'scripts/build_artifact.sh'
DEFAULT_ZIP_ARTIFACT = ''

# In‑memory build state
build_state = {
    'state': 'idle',   # idle | running | success | error | init_running | init_success
    'message': '',
    'artifact': '',
    'artifact2': '',
}
build_lock = threading.Lock()

# Optional extra fields mapped by patch id
EXTRA_FIELDS = {
    'bowden_length': [
        {'key': 'BOWDEN_LENGTH_MM', 'label': 'Bowden length (mm)', 'type': 'number', 'min': 10, 'max': 999, 'default': '700'},
    ],
    'bed_mesh_temp': [
        {'key': 'BED_MESH_TEMP', 'label': 'Bed mesh temperature (C)', 'type': 'number', 'min': 35, 'max': 99, 'default': '60'},
    ],
}


def load_patches():
    patches = []
    base = Path('cc-fw-tools/oc-patches')
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


def sync_cc_fw_tools(repo_url: str, branch: str):
    os.makedirs('cc-fw-tools', exist_ok=True)
    if os.path.isdir('cc-fw-tools/.git'):
        subprocess.run(['git', '-C', 'cc-fw-tools', 'remote', 'set-url', 'origin', repo_url], check=False)
        subprocess.run(['git', '-C', 'cc-fw-tools', 'fetch', 'origin', branch], check=False)
        subprocess.run(['git', '-C', 'cc-fw-tools', 'checkout', '-B', branch, f'origin/{branch}'], check=False)
        subprocess.run(['git', '-C', 'cc-fw-tools', 'reset', '--hard', f'origin/{branch}'], check=False)
    else:
        subprocess.run(['bash', '-lc', f'cd cc-fw-tools && git init && git remote add origin {repo_url} && git fetch --depth 1 origin {branch} && git checkout -B {branch} origin/{branch}'], check=False)
    subprocess.run(['git', 'lfs', 'install'], check=False)
    if os.path.isdir('cc-fw-tools/.git'):
        subprocess.run(['git', '-C', 'cc-fw-tools', 'lfs', 'pull'], check=False)


def current_source():
    try:
        if os.path.isdir('cc-fw-tools/.git'):
            r1 = subprocess.run(['git', '-C', 'cc-fw-tools', 'remote', 'get-url', 'origin'], capture_output=True, text=True, check=True)
            origin = (r1.stdout or '').strip()
            r2 = subprocess.run(['git', '-C', 'cc-fw-tools', 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True, check=True)
            branch = (r2.stdout or '').strip()
            return origin, branch
    except Exception:
        pass
    return None, None


@app.route('/', methods=['GET', 'POST'])
def index():
    errors = []
    form_values = {}
    patches = []

    # Repo/branch lists for UI
    branches_map = { r['key']: list_remote_branches(r['url'], r['key']) for r in REPOS }
    repo_selected = REPOS[0]['url']
    branch_selected = branches_map[REPOS[0]['key']][0] if branches_map[REPOS[0]['key']] else 'main'
    
    # Determine initialization state
    need_init = not os.path.isdir('cc-fw-tools/.git')
    if not need_init:
        # If already initialized, prefer actual current origin/branch for UI
        cur_repo, cur_branch = current_source()
        if cur_repo:
            repo_selected = cur_repo
        if cur_branch:
            branch_selected = cur_branch
        patches = load_patches()

    # Reflect background build state on GET renders
    download_ready = False
    artifact_name = ''
    error_text = None
    with build_lock:
        artifact2 = ''
        if build_state['state'] == 'success':
            artifact_name = build_state.get('artifact') or ''
            artifact2 = build_state.get('artifact2') or ''
            download_ready = True
        elif build_state['state'] == 'error':
            error_text = f"Build failed: {build_state['message']}"

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
                sync_cc_fw_tools(repo_selected, branch_selected)
                patches = load_patches()
                need_init = False
                return render_template('index.html', patches=patches, extra_fields=EXTRA_FIELDS, download_ready=False, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
            except Exception as e:
                error_msg = f"Source initialization failed: {str(e)}"
                return render_template('index.html', error=error_msg, patches=[], extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=True)

        # 2) Build path: ensure upstream synchronized first
        try:
            sync_cc_fw_tools(repo_selected, branch_selected)
            patches = load_patches()
            need_init = False

            # Merge patch_config (preserve upstream defaults)
            base_content = ''
            has_bootstrap_md5 = False
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as bcf:
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
                bootstrap = Path('cc-fw-tools/oc-patches/base-patch/OpenCentauri-bootstrap.tar.gz')
                if bootstrap.is_file():
                    md5 = hashlib.md5()
                    with open(bootstrap, 'rb') as bf:
                        for chunk in iter(lambda: bf.read(8192), b''):
                            md5.update(chunk)
                    override_lines.append(f"OC_BOOTSTRAP_MD5={md5.hexdigest()}")

            # Toggle MatchesEnv patches to explicit true/false (default true)
            for p in patches:
                policy = (p.get('execution_policy') or '').lower()
                pid = p.get('id') or ''
                if policy == 'matchesenv':
                    enabled_val = (request.form.get(f'enable__{pid}') or 'true').lower()
                    form_values[f'enable__{pid}'] = enabled_val
                    override_lines.append(f"{pid.upper()}={'true' if enabled_val=='true' else 'false'}")

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
                        override_lines.append(f"{field['key']}={val}")

            if errors:
                return render_template('index.html', errors=errors, patches=patches, extra_fields=EXTRA_FIELDS, download_ready=False, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)

            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                if base_content:
                    f.write(base_content.rstrip() + "\n\n")
                f.write("\n".join(override_lines) + "\n")

            # 3) Kick off build in the background
            adv_with_app = (request.form.get('adv_app_binary') == 'on')

            def _runner(version: str, with_app: bool):
                try:
                    args = ['/bin/bash', BUILD_SCRIPT, version]
                    if with_app:
                        args.append('1')
                    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    milestones = []
                    artifact_path = ''
                    app_path = ''
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            if proc.poll() is not None:
                                break
                            continue
                        L = line.strip()
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
                            build_state['message'] = '\n'.join(milestones[-6:]) if milestones else (build_state.get('message') or 'Starting...')
                    rescode = proc.wait()
                    if rescode == 0:
                        with build_lock:
                            build_state['state'] = 'success'
                            build_state['artifact'] = artifact_path or DEFAULT_ZIP_ARTIFACT
                            build_state['artifact2'] = app_path if with_app and app_path else ''
                    else:
                        with build_lock:
                            build_state['state'] = 'error'
                            build_state['message'] = f"Build failed with code {rescode}"
                            build_state['artifact'] = ''
                            build_state['artifact2'] = ''
                except Exception as ex:
                    with build_lock:
                        build_state['state'] = 'error'
                        build_state['message'] = str(ex)
                        build_state['artifact'] = ''
                        build_state['artifact2'] = ''

            with build_lock:
                if build_state['state'] == 'running':
                    return render_template('index.html', error='A build is already in progress. Please wait.', patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
                build_state['state'] = 'running'
                build_state['message'] = 'Starting...'

            threading.Thread(target=_runner, args=(fw_version, adv_with_app), daemon=True).start()

            return render_template('index.html', building=True, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=False)

        except subprocess.CalledProcessError as e:
            error_msg = f"Build Failed! Error: {e.stderr}"
            return render_template('index.html', error=error_msg, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
        except Exception as e:
            error_msg = f"An unexpected error occurred: {str(e)}"
            return render_template('index.html', error=error_msg, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)

    # GET
    if error_text:
        return render_template('index.html', error=error_text, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)
    if download_ready:
        return render_template('index.html', download_ready=True, artifact_name=artifact_name, artifact2_name=artifact2, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=False)
    return render_template('index.html', download_ready=False, patches=patches, extra_fields=EXTRA_FIELDS, form_values=form_values, repos=REPOS, branches_map=branches_map, repo_selected=repo_selected, branch_selected=branch_selected, need_init=need_init)


@app.route('/download/<path:filename>')
def download(filename):
    # Restrict to artifacts dir
    artifacts_root = os.path.abspath('artifacts')
    target_path = os.path.abspath(os.path.join('.', filename))
    if not target_path.startswith(artifacts_root) or not os.path.isfile(target_path):
        return ("Not Found", 404)

    resp = send_file(target_path, as_attachment=True)

    def _cleanup():
        try:
            os.remove(target_path)
        except Exception:
            pass
        try:
            with build_lock:
                if build_state.get('state') == 'success':
                    build_state['state'] = 'idle'
                    build_state['message'] = ''
                    build_state['artifact'] = ''
                    build_state['artifact2'] = ''
        except Exception:
            pass

    resp.call_on_close(_cleanup)
    return resp


@app.route('/status')
def status():
    with build_lock:
        return jsonify({
            'state': build_state['state'],
            'message': build_state['message'],
            'artifact': build_state.get('artifact') or '',
            'artifact2': build_state.get('artifact2') or '',
        })


@app.route('/init', methods=['POST'])
def init_source():
    repo_selected = request.form.get('repo_url', REPOS[0]['url'])
    # branch list is dependent on repo key; accept any value from UI
    branch_selected = request.form.get('branch_name', 'main')
    # prevent concurrent operations
    with build_lock:
        if build_state['state'] == 'running':
            return jsonify({'ok': False, 'error': 'A build is currently running. Please wait.'}), 409
        build_state['state'] = 'init_running'
        build_state['message'] = f'Loading source from {repo_selected} ({branch_selected})...'

    def _init_worker(repo_url: str, branch: str):
        try:
            # basic progress steps
            with build_lock:
                build_state['message'] = f'Cloning/fetching {branch}...'
            sync_cc_fw_tools(repo_url, branch)
            # small final update
            with build_lock:
                build_state['state'] = 'init_success'
                build_state['message'] = 'Source loaded. Patches available.'
        except Exception as ex:
            with build_lock:
                build_state['state'] = 'error'
                build_state['message'] = f'Init failed: {ex}'

    threading.Thread(target=_init_worker, args=(repo_selected, branch_selected), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/reset', methods=['POST'])
def reset_source():
    # Prevent reset while a build is running
    with build_lock:
        if build_state['state'] in ('running', 'init_running'):
            return jsonify({'ok': False, 'error': 'An operation is running. Please wait.'}), 409
        # Reset state
        build_state['state'] = 'idle'
        build_state['message'] = ''
        build_state['artifact'] = ''
        build_state['artifact2'] = ''
    try:
        # Remove the repo directory entirely to ensure a clean re-init next time
        import shutil
        if os.path.isdir('cc-fw-tools'):
            shutil.rmtree('cc-fw-tools', ignore_errors=True)
        # Recreate empty holder directory (optional, keeps UI consistent)
        os.makedirs('cc-fw-tools', exist_ok=True)
        return jsonify({'ok': True})
    except Exception as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
