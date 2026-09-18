"""Lima profile selection without creating VMs or installing packages."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def test_interrupted_extraction_is_not_reused(self):
        script = (ROOT / "scripts/install-lab-source.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            lab = Path(directory)
            old = lab / "sources/linux-1/linux-partial/kernel/sched/sched.h"
            old.parent.mkdir(parents=True)
            old.write_text("partial extraction")
            script = script.replace('. /etc/os-release', 'ID=debian; VERSION_CODENAME=trixie')
            script = script.replace('/var/lib/kexplore', str(lab))
            # Use lightweight guest command substitutes, with source extraction
            # failing after it has created a plausible kernel header.
            script = script.replace('apt-get() { command apt-get -o DPkg::Lock::Timeout=300 "$@"; }', '''
apt-get() {
  if [ "$1" = source ]; then
    echo SOURCE_ATTEMPTED
    mkdir -p linux-incomplete/kernel/sched
    touch linux-incomplete/kernel/sched/sched.h
    return 1
  fi
}
id() { echo 0; }
dpkg-query() { case "$*" in *source:Package*) echo linux ;; *) echo 1 ;; esac; }
apt-cache() { return 0; }
''')
            installer = lab / "installer.sh"
            installer.write_text('id() { echo 0; }\n' + script)
            result = subprocess.run(["bash", str(installer), "test-kernel"],
                                    env={**os.environ, "KEXPLORE_LAB_PROVISION": "1"},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SOURCE_ATTEMPTED", result.stdout, result.stderr)
            self.assertFalse((lab / "source.env").exists())
            self.assertEqual(list((lab / "sources/linux-1").glob("complete-*")), [])

    def shell(self, script, **variables):
        env = {key: value for key, value in os.environ.items()
               if key not in ("KEXPLORE_DISTRO", "KEXPLORE_VM")}
        return subprocess.run(
            ["bash", "-c", "source ./detect.sh; " + script], cwd=ROOT,
            env={**env, **variables}, capture_output=True, text=True,
        )

    def test_default_preserves_fedora_vm(self):
        result = self.shell("kexplore_lima_vm; kexplore_lima_template; kexplore_lima_server")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [
            "kernel-lab", "kexplore.yaml", "https://debuginfod.fedoraproject.org/",
        ])

    def test_distros_select_distinct_vms_and_servers(self):
        for distro, server in (("ubuntu", "https://debuginfod.ubuntu.com"),
                               ("debian", "https://debuginfod.debian.net")):
            with self.subTest(distro=distro):
                result = self.shell(
                    "kexplore_lima_vm; kexplore_lima_template; kexplore_debuginfod_for_backend lima",
                    KEXPLORE_DISTRO=distro,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.splitlines(), [
                    ("kernel-lab-ubuntu-26-04" if distro == "ubuntu" else f"kernel-lab-{distro}"),
                    f"kexplore-{distro}.yaml", server,
                ])

    def test_explicit_vm_name_is_preserved(self):
        result = self.shell("kexplore_lima_vm", KEXPLORE_DISTRO="ubuntu", KEXPLORE_VM="my-lab")
        self.assertEqual(result.stdout.strip(), "my-lab")

    def test_unknown_distro_is_rejected_even_with_vm_override(self):
        result = self.shell("kexplore_lima_vm", KEXPLORE_DISTRO="unknown", KEXPLORE_VM="my-lab")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("KEXPLORE_DISTRO must be", result.stderr)

    def test_existing_vm_cannot_be_repurposed(self):
        result = self.shell(
            'limactl() { echo "fedora 44"; }; kexplore_check_lima_distro my-lab',
            KEXPLORE_DISTRO="ubuntu",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runs fedora", result.stderr)

    def test_older_ubuntu_vm_is_not_silently_reused(self):
        result = self.shell(
            'limactl() { echo "ubuntu 24.04"; }; kexplore_check_lima_distro my-lab',
            KEXPLORE_DISTRO="ubuntu",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires 26.04", result.stderr)

    def test_existing_matching_vm_is_accepted(self):
        result = self.shell(
            'limactl() { echo "ubuntu 26.04"; }; kexplore_check_lima_distro my-lab',
            KEXPLORE_DISTRO="ubuntu",
        )
        self.assertEqual(result.returncode, 0)

    def test_older_fedora_and_debian_labs_are_rejected(self):
        for distro, release in (("fedora", "43"), ("debian", "12")):
            result = self.shell(
                f'limactl() {{ echo "{distro} {release}"; }}; kexplore_check_lima_distro my-lab',
                KEXPLORE_DISTRO=distro,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing VMs are not upgraded", result.stderr)

    def recovery(self, kernel_version="1", debug_version="1", distro="debian"):
        script = (ROOT / "scripts/provision-deb-lab.sh").read_text()
        selection = script.split("restart_required=0", 1)[1].split(
            'apt-get install -y "$debug_package"', 1)[0]
        mocks = '''
set -euo pipefail
dpkg() { echo arm64; }
apt-cache() {
  case "$1" in
    show) return 1 ;;
    depends) echo '  Depends: linux-image-6.12.test-cloud-arm64-dbg' ;;
    policy)
      case "$2" in
        *-dbg) echo "  Candidate: $DEBUG_VERSION" ;;
        *) echo "  Candidate: $KERNEL_VERSION" ;;
      esac ;;
  esac
}
apt-get() { printf 'INSTALL %s\\n' "$*"; }
restart_required=0
debug_package=linux-image-old-dbg
'''
        return subprocess.run(["bash", "-c", mocks + selection +
                               '\nprintf "restart=%s release=%s\\n" "$restart_required" "$release"'],
                              env={**os.environ, "ID": distro, "KERNEL_VERSION": kernel_version,
                                   "DEBUG_VERSION": debug_version}, capture_output=True, text=True)

    def test_debian_recovery_installs_exact_matching_pair(self):
        result = self.recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("linux-image-6.12.test-cloud-arm64=1", result.stdout)
        self.assertIn("linux-image-6.12.test-cloud-arm64-dbg=1", result.stdout)
        self.assertIn("restart=1 release=6.12.test-cloud-arm64", result.stdout)

    def test_debian_recovery_rejects_mismatched_versions_before_install(self):
        result = self.recovery(debug_version="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("INSTALL", result.stdout)

    def test_debian_recovery_is_not_applied_to_ubuntu(self):
        result = self.recovery(distro="ubuntu")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("INSTALL", result.stdout)

    def test_optional_sources_can_be_skipped_or_enabled_for_both_distros(self):
        script = (ROOT / "scripts/provision-deb-lab.sh").read_text()
        selection = 'if [ "${KEXPLORE_DOWNLOAD_SOURCE:-1}" = 1 ]; then' + script.split(
            'if [ "${KEXPLORE_DOWNLOAD_SOURCE:-1}" = 1 ]; then', 1)[1].split(
            'apt-get install -y bpftrace', 1)[0]
        for distro in ("ubuntu", "debian"):
            for enabled in ("0", "1"):
                with self.subTest(distro=distro, enabled=enabled):
                    result = subprocess.run(
                        ["bash", "-c", 'release=test; bash() { echo SOURCE_INSTALL; }; ' + selection],
                        env={**os.environ, "ID": distro, "KEXPLORE_DOWNLOAD_SOURCE": enabled},
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("SOURCE_INSTALL" in result.stdout, enabled == "1")


if __name__ == "__main__":
    unittest.main()
