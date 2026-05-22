"""Test creating a resource manager using PyVISA-Py as a backend."""

from pyvisa_py import highlevel

from pyvisa.highlevel import list_backends
from tests import BaseTestCase


class TestPyVisaLibrary(BaseTestCase):
    """Test generic property of PyVisaLibrary."""

    def test_list_backends(self):
        """Test listing backends."""
        assert "py" in list_backends()

    def test_debug_info(self):
        """Test generating debug infos for PyVISA-py."""
        infos = highlevel.PyVisaLibrary.get_debug_info()
        for key in ("Version",):
            assert key in infos
