#!/usr/bin/env python3
"""Plain-assert tests for the parts of muse-bridge.py that need no Muse: python3 tests/test_bridge.py"""
import importlib.util, os, tempfile

spec = importlib.util.spec_from_file_location("mb", os.path.join(os.path.dirname(__file__), "..", "bridge", "muse-bridge.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

# a denied command is remembered however it is spelled
assert mb.norm("rm -rf build") == mb.norm("rm  -rf ./build") == mb.norm('rm -rf "build/"')
assert mb.norm("rm -rf build") != mb.norm("rm -rf other")

ws = tempfile.mkdtemp()
for cmd in ["touch a.txt", "python3 test.py", "mkdir -p out && echo ok > out/a.txt", f"cat {ws}/x", "ls /usr/bin | head -3", "echo hi 2>/dev/null"]:
    assert not mb.RISKY.search(cmd) and not mb.leaves_workspace(cmd, ws), cmd
for cmd in ["cat ~/.ssh/config", "sudo dnf install x", "git push origin main", "rm -rf ~", "ls /etc/", "cat ../secret", "cat /home/someone/.bashrc", "cat 'unterminated"]:
    assert mb.RISKY.search(cmd) or mb.leaves_workspace(cmd, ws), cmd
print("ok")
