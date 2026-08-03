import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ToolchainPolicyTests(unittest.TestCase):
    def test_python_runtime_is_exactly_locked_and_installer_enforces_hashes(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        installer = (ROOT / "install_flowshift.ps1").read_text(encoding="utf-8")
        self.assertIn("pillow==12.3.0", requirements.lower())
        self.assertIn("pywebview==6.2.1", requirements.lower())
        for logical_line in requirements.replace("\\\n", "").splitlines():
            value = logical_line.strip()
            if value and not value.startswith(("#", "--hash")):
                self.assertRegex(value, r"^[a-z0-9_.-]+==[^ ]+", value)
        self.assertIn("--require-hashes", installer)
        self.assertNotRegex(installer, r"Python\.Python\.3\.12|3\.12\.9")
        self.assertIn("$MinPythonMinor = 10", installer)
        self.assertIn("$MaxPythonMinor = 14", installer)
        audit_requirements = (ROOT / "requirements-audit.txt").read_text(encoding="utf-8")
        self.assertIn("pip-audit==2.10.1", audit_requirements)
        self.assertIn("--hash=sha256:", audit_requirements)

    def test_webgui_manifest_has_exact_direct_dependencies(self):
        package = json.loads((ROOT / "webgui" / "package.json").read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, version in package[section].items():
                self.assertRegex(version, r"^\d+\.\d+\.\d+$", f"{name} is not exact")
        self.assertEqual(package["dependencies"]["react"], "19.2.8")
        self.assertEqual(package["dependencies"]["react-dom"], "19.2.8")
        self.assertEqual(package["devDependencies"]["vite"], "8.2.0")
        self.assertEqual(package["packageManager"], "npm@12.0.2")

    def test_ci_uses_immutable_actions_and_required_runtime_lanes(self):
        workflow_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        )
        for reference in re.findall(r"uses:\s*([^\s#]+)", workflow_text):
            self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", reference)
        for value in ("3.14.6", "24.18.1", "26.5.1", "pip_audit", "npm audit"):
            self.assertIn(value, workflow_text)
        self.assertNotRegex(workflow_text, r"actions/[^@]+@v\d")

    def test_dependency_automation_and_productive_payload_are_present(self):
        dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        for ecosystem in ("github-actions", "npm", "pip"):
            self.assertIn(f"package-ecosystem: {ecosystem}", dependabot)
        builder = (ROOT / "packaging" / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn("'web_api.py'", builder)


if __name__ == "__main__":
    unittest.main()
