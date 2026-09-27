import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never access the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
