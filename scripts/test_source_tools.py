#!/usr/bin/env python3
"""Three offline safety checks using temporary files, never the real project data."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True
from check_source import main as check_main
from export_source import export
from source_policy import PUBLIC_DEMO_ASSETS, content_rules, inspect_file, source_paths


class SourceToolsTests(unittest.TestCase):
    def test_export_excludes_private_files_and_its_own_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            public = {"README.md": "Source documentation\n", "app/main.py": "print('ok')\n",
                      "frontend/package-lock.json": "{}\n", ".env.example": "DEEPSEEK_API_KEY=\n"}
            private = [".env", ".env.backup", "data/db.sqlite3", "demo/sample.mp4",
                       ".local/exports/old.zip", ".venv/pyvenv.cfg", ".git/config",
                       "frontend/node_modules/package/index.js", "frontend/dist/index.html",
                       "app/__pycache__/main.pyc", "docs/private.pem", "unlisted/result.json"]
            for name, content in {**public, **dict.fromkeys(private, "private fixture")}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            original_read = Path.read_bytes
            def guarded_read(path):
                self.assertNotIn(path.relative_to(root).as_posix(), private)
                return original_read(path)
            output = root / ".local/exports/new.zip"
            with patch.object(Path, "read_bytes", guarded_read), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export(root, output), 0)
                second = root / ".local/exports/second.zip"
                self.assertEqual(export(root, second), 0)
            for archive_path in (output, second):
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(set(archive.namelist()), set(public))
                    for name, content in public.items():
                        self.assertEqual(archive.read(name), content.encode())
            self.assertEqual((root / ".env").read_text(), "private fixture")
            self.assertEqual((root / "app/main.py").read_text(), public["app/main.py"])

    def test_findings_are_redacted_and_dummy_keys_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "app").mkdir()
            token = "sk-" + "Aa1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p"
            machine_path = "/" + "Users" + "/" + "local-person" + "/private-project"
            (root / "app/bad.py").write_text(f'API_KEY = "{token}"\nSOURCE = "{machine_path}"\n')
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(export(root, root / ".local/exports/rejected.zip"), 1)
            self.assertNotIn(token, stream.getvalue())
            self.assertNotIn(machine_path, stream.getvalue())
            self.assertIn("obvious-token", stream.getvalue())
            self.assertIn("machine-absolute-path", stream.getvalue())
            self.assertFalse((root / ".local").exists())
            for dummy in ("TEST_ONLY_SECRET_DO_NOT_EXPOSE", "test-secret-placeholder-12345",
                          "sk-test-" + "0" * 30):
                self.assertEqual(content_rules(f'API_KEY = "{dummy}"'.encode()), set())

    def test_links_source_overwrites_and_tracked_env_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "app").mkdir()
            (root / "README.md").write_text("Source\n")
            (root / ".env").write_text("private fixture")
            (root / "app/link.py").symlink_to(root / ".env")
            rules, data = inspect_file(root, Path("app/link.py"))
            self.assertEqual(rules, {"symlink-not-allowed"})
            self.assertIsNone(data)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export(root, root / "source.zip"), 2)
                self.assertEqual(export(root, root / ".local/exports/rejected.zip"), 1)
            (root / "app/link.py").unlink()
            result = subprocess.CompletedProcess([], 0, stdout=b".env\0README.md\0", stderr=b"")
            stream = io.StringIO()
            with patch("check_source.subprocess.run", return_value=result), \
                 patch.object(sys, "argv", ["check_source.py", "--root", str(root), "--tracked"]), \
                 contextlib.redirect_stdout(stream):
                self.assertEqual(check_main(), 1)
            self.assertIn("private-environment-file", stream.getvalue())
            self.assertNotIn("private fixture", stream.getvalue())

    def test_readme_demo_is_tracked_and_other_videos_stay_private(self):
        expected = {
            Path("docs/demo") / name
            for name in (
                "产品展示.mp4", "产品展示.gif", "产品展示_poster.jpg",
                "story.mp4", "story.gif", "story_poster.jpg",
                "talking.mp4", "talking.gif", "talking_poster.jpg",
            )
        }
        self.assertEqual(PUBLIC_DEMO_ASSETS, expected)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "README.md").write_text("Source\n")
            demo_dir = root / "docs" / "demo"
            demo_dir.mkdir(parents=True)
            for relative in expected:
                (root / relative).write_bytes(b"\x00public-demo")
                rules, data = inspect_file(root, relative)
                self.assertEqual(rules, set(), relative.as_posix())
                self.assertIsNone(data)
            (demo_dir / "other.mp4").write_bytes(b"\x00other")
            (demo_dir / "extra.gif").write_bytes(b"\x00extra")
            private = root / "demo" / "sample.mp4"
            private.parent.mkdir()
            private.write_bytes(b"\x00private")
            (root / "docs" / "clip.mp4").write_bytes(b"\x00clip")
            self.assertEqual(inspect_file(root, Path("docs/demo/other.mp4"))[0], {"private-or-generated-path"})
            self.assertEqual(inspect_file(root, Path("docs/demo/extra.gif"))[0], {"private-or-generated-path"})
            self.assertEqual(inspect_file(root, Path("demo/sample.mp4"))[0], {"private-or-generated-path"})
            self.assertEqual(inspect_file(root, Path("docs/clip.mp4"))[0], {"private-or-generated-file"})
            names = {path.as_posix() for path in source_paths(root)}
            self.assertIn("README.md", names)
            for relative in expected:
                self.assertNotIn(relative.as_posix(), names)
            output = root / ".local" / "exports" / "demo.zip"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export(root, output), 0)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["README.md"])
            demo = demo_dir / "产品展示.mp4"
            demo.unlink()
            demo.symlink_to(private)
            self.assertEqual(inspect_file(root, Path("docs/demo/产品展示.mp4"))[0], {"symlink-not-allowed"})


if __name__ == "__main__":
    unittest.main()
