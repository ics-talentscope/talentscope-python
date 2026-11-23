import base64
import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

from services import saml

ISSUER = "https://talentscope.test/sp"
CALLBACK = "http://localhost:3000/api/employee/auth/saml/acs"


def make_idp():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


KEY_PEM, CERT_PEM = make_idp()
CERT_BODY = "".join(CERT_PEM.decode().splitlines()[1:-1])


def cfg(**over):
    base = {"issuer": ISSUER, "idp_cert": CERT_BODY, "callback_url": CALLBACK, "entry_point": "https://idp.test/saml2",
            "sp_private_key": "", "sp_cert": "", "nameid_format": "", "email_attribute": "", "app_url": "http://localhost:3000",
            "missing": [], "configured": True}
    base.update(over)
    return base


def build_response(audience=ISSUER, email="Jane.Doe@Corp.com", offset_minutes=0, sign=True, recipient=CALLBACK,
                   assertion_id="_a1", key_pem=KEY_PEM):
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset_minutes)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r1" Version="2.0" IssueInstant="{fmt(now)}">
<saml:Issuer>https://idp.test</saml:Issuer>
<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
<saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{fmt(now)}">
<saml:Issuer>https://idp.test</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData Recipient="{recipient}" NotOnOrAfter="{fmt(now + dt.timedelta(minutes=5))}"/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{fmt(now - dt.timedelta(minutes=1))}" NotOnOrAfter="{fmt(now + dt.timedelta(minutes=5))}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AttributeStatement><saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion></samlp:Response>"""
    root = etree.fromstring(xml.encode())
    if sign:
        assertion = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        signer = XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256", digest_algorithm="sha256",
                           c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
        signed = signer.sign(assertion, key=key_pem, cert=CERT_PEM, reference_uri="#" + assertion_id)
        issuer = signed.find("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer")
        sig = signed.find("{http://www.w3.org/2000/09/xmldsig#}Signature")
        signed.remove(sig)
        issuer.addnext(sig)
        root.replace(assertion, signed)
    return base64.b64encode(etree.tostring(root)).decode()


@pytest.fixture(autouse=True)
def clear_replay():
    saml.reset_replay_cache()
    saml.reset_metadata_cache()


def test_valid_signed_assertion_yields_profile():
    profile = saml.validate_post_response(build_response(), cfg())
    assert profile["nameID"] == "Jane.Doe@Corp.com"
    assert saml.profile_emails(profile, cfg()) == ["jane.doe@corp.com"]
    assert saml.profile_local_part(profile, cfg()) == "jane.doe"


def test_certificate_rotation_uses_any_configured_signing_certificate():
    rotated_cfg = cfg(idp_certs=["invalid-certificate", CERT_BODY])

    profile = saml.validate_post_response(build_response(), rotated_cfg)

    assert profile["nameID"] == "Jane.Doe@Corp.com"


def test_unsigned_assertion_rejected():
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(build_response(sign=False), cfg())


def test_wrong_signing_key_rejected():
    other_key, _ = make_idp()
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(build_response(key_pem=other_key), cfg())


def test_tampered_assertion_rejected():
    raw = base64.b64decode(build_response()).replace(b"Jane.Doe@Corp.com", b"admin@corp.com")
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(base64.b64encode(raw).decode(), cfg())


def test_audience_expiry_and_recipient_checks():
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(build_response(audience="https://other"), cfg())
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(build_response(offset_minutes=-30), cfg())
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(build_response(recipient="http://evil/acs"), cfg())


def test_replay_rejected():
    payload = build_response()
    saml.validate_post_response(payload, cfg())
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(payload, cfg())


def test_doctype_rejected():
    xml = b'<!DOCTYPE x [<!ENTITY a "b">]><samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"/>'
    with pytest.raises(saml.SamlError):
        saml.validate_post_response(base64.b64encode(xml).decode(), cfg())


def test_authorize_url_and_metadata():
    url = saml.build_authorize_url("/employee", cfg())
    parsed = urlparse(url)
    assert parsed.netloc == "idp.test"
    assert "SAMLRequest" in parse_qs(parsed.query) and parse_qs(parsed.query)["RelayState"] == ["/employee"]
    meta = saml.build_metadata(cfg())
    assert ISSUER in meta and CALLBACK in meta and 'WantAssertionsSigned="true"' in meta


def test_parse_metadata_signing_certs_ignores_encryption_keys():
    xml = f"""<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
        xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
      <md:IDPSSODescriptor>
        <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>
          <ds:X509Certificate>{CERT_BODY}</ds:X509Certificate>
        </ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
        <md:KeyDescriptor use="encryption"><ds:KeyInfo><ds:X509Data>
          <ds:X509Certificate>encryption-cert</ds:X509Certificate>
        </ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
      </md:IDPSSODescriptor>
    </md:EntityDescriptor>"""

    assert saml.parse_metadata_certs(xml.encode()) == [CERT_BODY]


def test_metadata_certs_fetch_from_microsoft_url(monkeypatch):
    import httpx

    xml = f"""<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
        xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
      <md:IDPSSODescriptor><md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>
        <ds:X509Certificate>{CERT_BODY}</ds:X509Certificate>
      </ds:X509Data></ds:KeyInfo></md:KeyDescriptor></md:IDPSSODescriptor>
    </md:EntityDescriptor>""".encode()

    class Response:
        content = xml

        def raise_for_status(self):
            pass

    saml.reset_metadata_cache()
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    certs, error = saml.metadata_certs("https://login.microsoftonline.com/tenant/federationmetadata.xml")

    assert certs == [CERT_BODY]
    assert error == ""


def test_metadata_configuration_enables_sso_without_static_certificate(monkeypatch):
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "12345678-1234-1234-1234-123456789012")
    monkeypatch.setenv("SAML_ISSUER", ISSUER)
    monkeypatch.setenv("SAML_IDP_CERT", "")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://login.microsoftonline.com/tenant/metadata")
    monkeypatch.setenv("SAML_ENABLED", "1")
    monkeypatch.setenv("SAML_ADMIN_SSO", "1")
    monkeypatch.setattr(saml, "metadata_certs", lambda _url: ([CERT_BODY], ""))

    config = saml.get_config()

    assert config["configured"] is True
    assert config["enabled"] is True
    assert config["idp_cert"] == CERT_BODY
    assert config["admin_sso"] is True

    monkeypatch.setenv("SAML_ENABLED", "0")
    assert saml.get_config()["enabled"] is False


def test_saml_status_reports_enabled_when_metadata_is_configured(client, monkeypatch):
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "12345678-1234-1234-1234-123456789012")
    monkeypatch.setenv("SAML_ISSUER", ISSUER)
    monkeypatch.setenv("SAML_IDP_CERT", "")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://login.microsoftonline.com/tenant/metadata")
    monkeypatch.setenv("SAML_ENABLED", "1")
    monkeypatch.setattr(saml, "metadata_certs", lambda _url: ([CERT_BODY], ""))

    response = client.get("/api/employee/auth/saml/status")

    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_acs_end_to_end(client, store, monkeypatch):
    monkeypatch.setenv("SAML_ENABLED", "1")
    monkeypatch.setenv("SAML_ISSUER", ISSUER)
    monkeypatch.setenv("SAML_IDP_CERT", CERT_BODY)
    monkeypatch.setenv("SAML_ENTRY_POINT", "https://idp.test/saml2")
    monkeypatch.setenv("SAML_IDP_METADATA_URL", "")
    monkeypatch.setenv("SAML_IDP_ENTITY_ID", "https://idp.test")
    # This test posts an IdP-initiated response without first calling /saml.
    monkeypatch.setenv("SAML_ALLOW_IDP_INITIATED", "1")
    store.seed("employees", [{"id": "e1", "employee_id": "EMP9", "email": "jane.doe@corp.com", "full_name": "Jane", "role": "employee"}])
    res = client.post("/api/employee/auth/saml/acs", data={"SAMLResponse": build_response()}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].endswith("/employee/sso-complete")
    handoff = client.get("/api/employee/auth/microsoft/handoff")
    assert handoff.status_code == 200 and handoff.json()["token"]
    assert client.get("/api/employee/auth/microsoft/handoff").status_code == 401
    unknown = client.post("/api/employee/auth/saml/acs", data={"SAMLResponse": build_response(email="nobody@corp.com", assertion_id="_a2")},
                          follow_redirects=False)
    assert "sso_error" in unknown.headers["location"]


def test_start_not_configured_redirects_with_message(client, monkeypatch):
    for key in ("MICROSOFT_TENANT_ID", "SAML_ISSUER", "SAML_IDP_CERT", "SAML_IDP_METADATA_URL", "SAML_ENTRY_POINT"):
        monkeypatch.delenv(key, raising=False)
    res = client.get("/api/employee/auth/saml", follow_redirects=False)
    assert "sso_error" in res.headers["location"] and "SAML_ISSUER" in res.headers["location"].replace("%20", " ")
