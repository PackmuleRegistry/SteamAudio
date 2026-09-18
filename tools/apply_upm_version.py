#!/usr/bin/env python3
"""
Downloads a published Steam Audio release from valvesoftware/steam-audio,
applies the UPM (Unity Package Manager) conversion patch used by this repo,
and stages the result under Package/ for commit/tag/push.

Usage:
    python tools/apply_upm_version.py 4.0.3 [--dry-run] [--keep-downloads]

With --dry-run the working tree is updated but nothing is committed/tagged/
pushed, so the result can be reviewed (e.g. via `git status` / `git diff`)
before running again without the flag.
"""
import argparse
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "package"

UPSTREAM_REPO = "valvesoftware/steam-audio"

# Maps upstream steamaudio_fmod_<ver>.zip lib/<platform> dirs to this repo's
# package/Plugins/FMOD/lib/<platform> layout. Only the FMOD spatializer plugin
# binaries are carried over (not the core phonon binaries, which come from the
# steamaudio_unity_<ver>.zip .unitypackage instead).
FMOD_DIR_MAP = {
    "android-armv7": "android/armeabi-v7a",
    "android-armv8": "android/arm64-v8a",
    "android-x86": "android/x86",
    "linux-x64": "linux/x86_64",
    "linux-x86": "linux/x86",
    "osx": "mac",
    "windows-x64": "win/x86_64",
    "windows-x86": "win/x86",
}
# Only these file(s)/pattern(s) per source dir are copied (the FMOD spatializer
# plugin, not the full phonon core library which ships with the Unity package).
FMOD_KEEP_NAMES = {"phonon_fmod.dll", "phonon_fmod.bundle", "libphonon_fmod.so"}

SETTINGS_CS_REL = Path("Plugins/SteamAudio/Scripts/Runtime/SteamAudioSettings.cs")
OLD_SETTINGS_LINE = '                        AssetDatabase.CreateAsset(sSingleton, "Assets/Plugins/SteamAudio/Resources/SteamAudioSettings.asset");\n'
NEW_SETTINGS_LINES = (
    "                        // SteamAudioUnityPackage: Changed due to git-sourced packages' immutability.\n"
    "                        /*\n"
    '                        AssetDatabase.CreateAsset(sSingleton, "Assets/Plugins/SteamAudio/Resources/SteamAudioSettings.asset");\n'
    "                        */\n"
)


def run(cmd, cwd=None, check=True):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd or REPO_ROOT, check=check)


def download_release_assets(version: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    tag = f"v{version}"
    patterns = [f"steamaudio_unity_{version}.zip", f"steamaudio_fmod_{version}.zip"]
    for p in patterns:
        target = dest / p
        if target.exists():
            continue
        run([
            "gh", "release", "download", tag,
            "--repo", UPSTREAM_REPO,
            "-p", p,
            "-D", str(dest),
        ])
    return dest / patterns[0], dest / patterns[1]


def extract_unitypackage(unitypackage_path: Path, out_assets_dir: Path):
    """Reconstructs an Assets/... tree from a .unitypackage (gzipped tar of
    GUID-named entries containing asset/asset.meta/pathname)."""
    out_assets_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(unitypackage_path, "r:gz") as tf:
        members = {m.name: m for m in tf.getmembers()}
        guids = sorted({n.split("/")[0] for n in members if "/" in n})
        for guid in guids:
            pathname_member = members.get(f"{guid}/pathname")
            if pathname_member is None:
                continue
            rel_path = tf.extractfile(pathname_member).read().decode("utf-8", errors="replace").strip()
            if not rel_path.startswith("Assets/"):
                continue
            dest_path = out_assets_dir / Path(rel_path).relative_to("Assets")
            asset_member = members.get(f"{guid}/asset")
            if asset_member is not None:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(asset_member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            else:
                # Directory-only entry.
                dest_path.mkdir(parents=True, exist_ok=True)


def sync_steamaudio_scripts(assets_dir: Path):
    src = assets_dir / "Plugins" / "SteamAudio"
    dst = PACKAGE_DIR / "Plugins" / "SteamAudio"
    if not src.is_dir():
        raise RuntimeError(f"Expected extracted path missing: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def patch_settings_cs():
    path = PACKAGE_DIR / SETTINGS_CS_REL
    text = path.read_text(encoding="utf-8")
    if "SteamAudioUnityPackage: Changed due to git-sourced packages' immutability." in text:
        print(f"[skip] {path} already patched")
        return
    if OLD_SETTINGS_LINE not in text:
        raise RuntimeError(
            f"Could not find expected line to patch in {path}; "
            "upstream file layout may have changed, patch manually."
        )
    text = text.replace(OLD_SETTINGS_LINE, NEW_SETTINGS_LINES)
    path.write_text(text, encoding="utf-8")
    print(f"[patched] {path}")


def sync_fmod_libs(fmod_zip: Path, work_dir: Path):
    extract_dir = work_dir / "fmod_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    with zipfile.ZipFile(fmod_zip) as zf:
        zf.extractall(extract_dir)

    src_lib_root = next(extract_dir.glob("*/lib"))
    dst_lib_root = PACKAGE_DIR / "Plugins" / "FMOD" / "lib"

    for src_name, dst_rel in FMOD_DIR_MAP.items():
        src_dir = src_lib_root / src_name
        if not src_dir.is_dir():
            print(f"[warn] missing upstream fmod dir: {src_dir}")
            continue
        dst_dir = dst_lib_root / dst_rel
        for item in src_dir.iterdir():
            if item.name not in FMOD_KEEP_NAMES:
                continue
            dst_item = dst_dir / item.name
            if dst_item.exists():
                if dst_item.is_dir():
                    shutil.rmtree(dst_item)
                else:
                    dst_item.unlink()
            dst_dir.mkdir(parents=True, exist_ok=True)
            if item.is_dir():
                shutil.copytree(item, dst_item)
            else:
                shutil.copy2(item, dst_item)


def bump_package_json(version: str):
    path = PACKAGE_DIR / "package.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[updated] {path} -> version {version}")


def apply_version(version: str, keep_downloads: bool):
    work_dir = Path(tempfile.gettempdir()) / f"steamaudio_upm_{version}"
    work_dir.mkdir(parents=True, exist_ok=True)

    unity_zip, fmod_zip = download_release_assets(version, work_dir / "downloads")

    unity_extract_dir = work_dir / "unity_zip"
    if unity_extract_dir.exists():
        shutil.rmtree(unity_extract_dir)
    with zipfile.ZipFile(unity_zip) as zf:
        zf.extractall(unity_extract_dir)
    unitypackage = next(unity_extract_dir.rglob("*.unitypackage"))

    assets_dir = work_dir / "assets_tree"
    if assets_dir.exists():
        shutil.rmtree(assets_dir)
    extract_unitypackage(unitypackage, assets_dir)

    sync_steamaudio_scripts(assets_dir)
    patch_settings_cs()
    sync_fmod_libs(fmod_zip, work_dir)
    bump_package_json(version)

    if not keep_downloads:
        shutil.rmtree(work_dir, ignore_errors=True)


def git_commit_tag_push(version: str, dry_run: bool):
    tag = f"v{version}"
    if dry_run:
        print(f"[dry-run] would commit, tag {tag}, and push")
        run(["git", "status", "--short"])
        return
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"Version: {tag}"])
    run(["git", "tag", tag])
    run(["git", "push", "origin", "master"])
    run(["git", "push", "origin", tag])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Version number without leading v, e.g. 4.0.3")
    parser.add_argument("--dry-run", action="store_true", help="Stage files but do not commit/tag/push")
    parser.add_argument("--keep-downloads", action="store_true", help="Keep temp download/extraction dir")
    args = parser.parse_args()

    apply_version(args.version, args.keep_downloads)
    git_commit_tag_push(args.version, args.dry_run)


if __name__ == "__main__":
    main()
