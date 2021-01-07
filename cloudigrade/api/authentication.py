"""Custom Cloudigrade Authentication Policies."""
import base64
import json
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.utils.translation import gettext as _
from rest_framework import HTTP_HEADER_ENCODING
from rest_framework.authentication import BaseAuthentication, exceptions

logger = logging.getLogger(__name__)


def parse_requests_header(request):
    """
    Get relevant information from the given request's identity header.

    Returns:
        (str, str, bool): the tuple of auth_header, account_number, is_org_admin
            fields. If the auth header is not present, this function returns
            (None, None, False)

    Raises:
        AuthenticationFailed: If any of the fields are malformed.
    """
    insights_request_id = request.META.get(settings.INSIGHTS_REQUEST_ID_HEADER, None)
    logger.info(
        _("Authenticating via insights, INSIGHTS_REQUEST_ID: %s"),
        insights_request_id,
    )

    auth_header = request.META.get(settings.INSIGHTS_IDENTITY_HEADER, None)

    # Can't authenticate if there isn't a header
    if not auth_header:
        return None, None, False
    try:
        auth = json.loads(base64.b64decode(auth_header).decode(HTTP_HEADER_ENCODING))

    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.info(_("Authentication Failed: identity header parsing error %s"), e)
        raise exceptions.AuthenticationFailed(
            _("Authentication Failed: invalid identity header- {error}").format(error=e)
        )

    if settings.VERBOSE_INSIGHTS_IDENTITY_HEADER_LOGGING:
        # Important note: this setting defaults to False and generally should remain
        # as False except for very special and *temporary* circumstances when we need
        # to investigate unusual request handling.
        logger.info(_("Decoded identity header: %s"), str(auth))

    # If account_number is not in header, authentication fails
    try:
        account_number = auth["identity"]["account_number"]
    except KeyError:
        logger.info(
            _(
                "Authentication Failed: "
                "account_number not contained "
                "in identity header %s."
            ),
            auth_header,
        )
        raise exceptions.AuthenticationFailed(
            _(
                "Authentication Failed: invalid identity header- "
                "missing user account_number field"
            )
        )

    is_org_admin = auth["identity"].get("user", {}).get("is_org_admin")
    logger.info(
        _(
            "identity header has account_number '%(account_number)s' "
            "is_org_admin '%(is_org_admin)s'"
        ),
        {"account_number": account_number, "is_org_admin": is_org_admin},
    )
    return auth_header, account_number, is_org_admin


class IdentityHeaderAuthentication(BaseAuthentication):
    """
    Authentication class that uses identity headers to find Django Users.

    This authentication requires the identity header to exist with an identity having
    org_admin enabled. If we cannot find a User matching the identity, then
    authentication fails and returns None.
    """

    require_org_admin = True
    require_account_number = True
    require_user = True
    create_user = False

    def assert_org_admin(self, account_number, is_org_admin):
        """
        Assert org_admin is set if required.

        This functionality arguably belongs in a Permission class, not an Authentication
        class, but it's simply convenient to include here because this assertion
        requires parsing the identity header, and we've already done that here to get
        the identity account number.
        """
        if self.require_org_admin and not is_org_admin:
            logger.info(
                _(
                    "Authentication Failed: identity account number %(account_number)s"
                    "is not org admin in identity header."
                ),
                {"account_number": account_number},
            )
            raise exceptions.PermissionDenied(_("User must be an org admin."))

    def get_user(self, account_number):
        """Get the Django User for the account number."""
        if not account_number:
            if self.require_account_number:
                raise exceptions.PermissionDenied(
                    _(
                        "Authentication Failed: identity account number is required "
                        "but was not present in request."
                    )
                )
            else:
                return None

        user = None
        if self.create_user:
            user, created = User.objects.get_or_create(username=account_number)
            if created:
                user.set_unusable_password()
                logger.info(
                    _("Username %s was not found and has been created."),
                    account_number,
                )
        elif User.objects.filter(username=account_number).exists():
            user = User.objects.get(username=account_number)
            logger.info(
                _("Authentication found user with username %(account_number)s"),
                {"account_number": account_number},
            )
        elif self.require_user:
            message = _(
                "Authentication Failed: user with account number {username} "
                "does not exist."
            ).format(username=account_number)
            logger.info(message)
            raise exceptions.AuthenticationFailed()
        else:
            logger.info(
                _("Username %s was not found but is not required."),
                account_number,
            )
        logger.debug(
            _("Authenticated user for username %(account_number)s is %(user)s"),
            {"account_number": account_number, "user": user},
        )
        return user

    def authenticate(self, request):
        """Authenticate the request using the identity header."""
        auth_header, account_number, is_org_admin = parse_requests_header(request)

        # Can't authenticate if there isn't a header
        if not auth_header:
            if not self.require_account_number and not self.require_org_admin:
                return None
            else:
                raise exceptions.AuthenticationFailed

        self.assert_org_admin(account_number, is_org_admin)
        if user := self.get_user(account_number):
            return user, True
        return None


class IdentityHeaderAuthenticationUserNotRequired(IdentityHeaderAuthentication):
    """
    Authentication class that does not require a User to exist for the account number.

    This authentication checks for the identity header and requires the identity to
    exist with an account number and with org_admin enabled. However, this does not
    require that a User matching the identity's account number exists.

    This variant exists because at least one public API (sysconfig) needs to have access
    restricted to an authenticated Red Hat identity before a corresponding User may have
    been created within our system.
    """

    require_org_admin = True
    require_account_number = True
    require_user = False
    create_user = False


class IdentityHeaderAuthenticationInternal(IdentityHeaderAuthentication):
    """
    Authentication class that only optionally uses the identity header.

    This authentication checks for the identity header but does not require the identity
    to exist or to have org_admin enabled. If we cannot find a User matching the header
    identity, then authentication fails and returns None. We expect the downstream view
    to determine if access should be allowed if no authentication exists.

    This "optional" variant exists because internal Red Hat Cloud services do not
    consistently set the org_admin value, and we want to grant generally broad access to
    our internal APIs.
    """

    require_org_admin = False
    require_account_number = False
    require_user = False
    create_user = False


class IdentityHeaderAuthenticationInternalCreateUser(IdentityHeaderAuthentication):
    """
    Authentication class that uses identity header to creates Users.

    This authentication checks for the identity header but does not require the identity
    to have org_admin enabled. If we cannot find a User matching the header's identity,
    then we create a new User from the identity header's account number.
    """

    require_org_admin = False
    require_account_number = True
    require_user = False
    create_user = True
