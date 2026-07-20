from __future__ import annotations

import ssl
from importlib import resources


def build_gh_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("AES256-GCM-SHA384")
    certificate = resources.files("rental_monitor").joinpath(
        "certs/sectigo_rsa_organization_validation_secure_server_ca.pem"
    )
    with resources.as_file(certificate) as certificate_path:
        context.load_verify_locations(cafile=certificate_path)
    return context
