"""
py2app build configuration for ID5 Batch Processor.

Usage:
    python setup.py py2app
"""

from setuptools import setup

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "includes": [
        "process_batches",
        "auth",
        "pdfplumber",
        "pypdf",
        "dotenv",
        "cffi",
        "_cffi_backend",
        # keyring backend discovery does not survive py2app's static analysis,
        # so name the macOS Keychain backend explicitly. Without this the
        # bundled .app silently falls through to the 0600 file fallback every
        # launch. fail backend is included so keyring can still import on a
        # box where Keychain access is denied (CI, headless macOS).
        "keyring.backends.macOS",
        "keyring.backends.fail",
        "keyring.backends.chainer",
        "keyring.backends.null",
    ],
    "packages": [
        "pdfplumber",
        "pypdf",
        "charset_normalizer",
        "pdfminer",
        "cryptography",
        "cffi",
        "requests",
        "keyring",
    ],
}

setup(
    app=APP,
    name="ID5 Batch Processor",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
