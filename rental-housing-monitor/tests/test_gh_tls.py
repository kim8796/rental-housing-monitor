import ssl

from rental_monitor.gh_tls import build_gh_ssl_context


def test_gh_context_keeps_verification_and_adds_required_cipher_and_ca() -> None:
    context = build_gh_ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.maximum_version is ssl.TLSVersion.TLSv1_2
    assert "AES256-GCM-SHA384" in {cipher["name"] for cipher in context.get_ciphers()}
    subjects = str(context.get_ca_certs())
    assert "Sectigo RSA Organization Validation Secure Server CA" in subjects
