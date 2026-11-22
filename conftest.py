"""Keeps a bare ``pytest`` at the repository root on the service's tests.

``clients/`` holds separate packages with their own test setups. The Python
client there is also named ``calotype`` and must be tested from
``clients/python`` (see its README), never together with the service.
"""

collect_ignore = ["clients"]
