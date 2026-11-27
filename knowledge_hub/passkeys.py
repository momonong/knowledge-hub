"""WebAuthn options and verification for the Knowledge Hub relying party."""

from hmac import compare_digest
from struct import error as StructError

try:
    from cryptography.exceptions import UnsupportedAlgorithm
    from fido2.server import Fido2Server
    from fido2.utils import websafe_decode
    from fido2.webauthn import (AttestationConveyancePreference, AttestedCredentialData,
                               AuthenticationResponse, AuthenticatorAttachment,
                               PublicKeyCredentialRpEntity, PublicKeyCredentialUserEntity,
                               RegistrationResponse, ResidentKeyRequirement, UserVerificationRequirement)
except ImportError:
    Fido2Server = None


RP_ID = 'momonong.me'
ORIGINS = (
    'https://momonong.me',
    'android:apk-key-hash:5Z0ZXgCQakigsnXNBuEimYYqXaS-zuJUefzHwkcKTv4',
)
CHALLENGE_SECONDS = 180


class InvalidPasskey(ValueError):
    pass


def _server():
    if Fido2Server is None:
        return None
    server = Fido2Server(PublicKeyCredentialRpEntity(id=RP_ID, name='Knowledge Hub'),
                         attestation=AttestationConveyancePreference.NONE,
                         verify_origin=lambda origin: origin in ORIGINS)
    server.timeout = CHALLENGE_SECONDS * 1000
    return server


SERVER = _server()


def _check_client_data(credential):
    if credential.response.client_data.cross_origin is not False or credential.type != 'public-key':
        raise InvalidPasskey()


def registration_options(name, device_id):
    # Each device has its own user handle so an authenticator can discover multiple enrollments.
    user = PublicKeyCredentialUserEntity(id=websafe_decode(device_id), name=name, display_name=name)
    options, state = SERVER.register_begin(user, resident_key_requirement=ResidentKeyRequirement.REQUIRED,
                                           user_verification=UserVerificationRequirement.REQUIRED,
                                           authenticator_attachment=AuthenticatorAttachment.PLATFORM)
    return dict(options), state


def authentication_options():
    options, state = SERVER.authenticate_begin(user_verification=UserVerificationRequirement.REQUIRED)
    return dict(options), state


def verify_registration(state, credential):
    try:
        parsed = RegistrationResponse.from_dict(credential)
        _check_client_data(parsed)
        data = SERVER.register_complete(state, parsed)
        attested = data.credential_data
        if (attested is None or not 1 <= len(attested.credential_id) <= 1023
                or parsed.raw_id != attested.credential_id
                or attested.public_key.ALGORITHM not in {item.alg for item in SERVER.allowed_algorithms}
                or (data.is_backed_up() and not data.is_backup_eligible())):
            raise InvalidPasskey()
        return data
    except (AttributeError, AssertionError, IndexError, KeyError, OverflowError, RecursionError,
            StructError, TypeError, UnsupportedAlgorithm, ValueError):
        raise InvalidPasskey() from None


def parse_assertion(credential):
    try:
        parsed = AuthenticationResponse.from_dict(credential)
        _check_client_data(parsed)
        return parsed
    except (AttributeError, IndexError, KeyError, OverflowError, RecursionError, StructError, TypeError, ValueError):
        raise InvalidPasskey() from None


def verify_assertion(state, assertion, device):
    stored = AttestedCredentialData(device['public_key'])
    data = assertion.response.authenticator_data
    user_handle = assertion.response.user_handle
    if (user_handle is None or not compare_digest(user_handle, websafe_decode(device['id']))
            or data.is_backup_eligible() != device['backup_eligible']
            or (data.is_backed_up() and not data.is_backup_eligible())):
        raise InvalidPasskey()
    try:
        SERVER.authenticate_complete(state, [stored], assertion)
    except (KeyError, TypeError, UnsupportedAlgorithm, ValueError):
        raise InvalidPasskey() from None
    # Devices that implement counters must increment them. Synced passkeys can use zero
    # or independent counters, so their signatures and challenge provide replay protection.
    if (not data.is_backup_eligible() and (data.counter or device['sign_count'])
            and data.counter <= device['sign_count']):
        raise InvalidPasskey()
    return max(data.counter, device['sign_count'])
