"""
An API for retrieving user account information.

For additional information and historical context, see:
https://openedx.atlassian.net/wiki/display/TNL/User+API
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from edx_rest_framework_extensions.authentication import JwtAuthentication
from rest_framework import permissions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet
from six import text_type
from social_django.models import UserSocialAuth

from openedx.core.djangoapps.user_api.preferences.api import update_email_opt_in
from openedx.core.lib.api.authentication import (
    SessionAuthenticationAllowInactiveUser,
    OAuth2AuthenticationAllowInactiveUser,
)
from openedx.core.lib.api.parsers import MergePatchParser
from student.models import User, get_potentially_retired_user_by_username_and_hash, get_retired_email_by_email

from .api import get_account_settings, update_account_settings
from .permissions import CanDeactivateUser, CanRetireUser
from .signals import USER_RETIRE_MAILINGS
from ..errors import UserNotFound, UserNotAuthorized, AccountUpdateError, AccountValidationError
from ..models import UserOrgTag


class AccountViewSet(ViewSet):
    """
        **Use Cases**

            Get or update a user's account information. Updates are supported
            only through merge patch.

        **Example Requests**

            GET /api/user/v1/me[?view=shared]
            GET /api/user/v1/accounts?usernames={username1,username2}[?view=shared]
            GET /api/user/v1/accounts/{username}/[?view=shared]

            PATCH /api/user/v1/accounts/{username}/{"key":"value"} "application/merge-patch+json"

        **Response Values for GET requests to the /me endpoint**
            If the user is not logged in, an HTTP 401 "Not Authorized" response
            is returned.

            Otherwise, an HTTP 200 "OK" response is returned. The response
            contains the following value:

            * username: The username associated with the account.

        **Response Values for GET requests to /accounts endpoints**

            If no user exists with the specified username, an HTTP 404 "Not
            Found" response is returned.

            If the user makes the request for her own account, or makes a
            request for another account and has "is_staff" access, an HTTP 200
            "OK" response is returned. The response contains the following
            values.

            * bio: null or textual representation of user biographical
              information ("about me").
            * country: An ISO 3166 country code or null.
            * date_joined: The date the account was created, in the string
              format provided by datetime. For example, "2014-08-26T17:52:11Z".
            * email: Email address for the user. New email addresses must be confirmed
              via a confirmation email, so GET does not reflect the change until
              the address has been confirmed.
            * gender: One of the following values:

                * null
                * "f"
                * "m"
                * "o"

            * goals: The textual representation of the user's goals, or null.
            * is_active: Boolean representation of whether a user is active.
            * language: The user's preferred language, or null.
            * language_proficiencies: Array of language preferences. Each
              preference is a JSON object with the following keys:

                * "code": string ISO 639-1 language code e.g. "en".

            * level_of_education: One of the following values:

                * "p": PhD or Doctorate
                * "m": Master's or professional degree
                * "b": Bachelor's degree
                * "a": Associate's degree
                * "hs": Secondary/high school
                * "jhs": Junior secondary/junior high/middle school
                * "el": Elementary/primary school
                * "none": None
                * "o": Other
                * null: The user did not enter a value

            * mailing_address: The textual representation of the user's mailing
              address, or null.
            * name: The full name of the user.
            * profile_image: A JSON representation of a user's profile image
              information. This representation has the following keys.

                * "has_image": Boolean indicating whether the user has a profile
                  image.
                * "image_url_*": Absolute URL to various sizes of a user's
                  profile image, where '*' matches a representation of the
                  corresponding image size, such as 'small', 'medium', 'large',
                  and 'full'. These are configurable via PROFILE_IMAGE_SIZES_MAP.

            * requires_parental_consent: True if the user is a minor
              requiring parental consent.
            * social_links: Array of social links. Each
              preference is a JSON object with the following keys:

                * "platform": A particular social platform, ex: 'facebook'
                * "social_link": The link to the user's profile on the particular platform

            * username: The username associated with the account.
            * year_of_birth: The year the user was born, as an integer, or null.
            * account_privacy: The user's setting for sharing her personal
              profile. Possible values are "all_users" or "private".
            * accomplishments_shared: Signals whether badges are enabled on the
              platform and should be fetched.

            For all text fields, plain text instead of HTML is supported. The
            data is stored exactly as specified. Clients must HTML escape
            rendered values to avoid script injections.

            If a user who does not have "is_staff" access requests account
            information for a different user, only a subset of these fields is
            returned. The returns fields depend on the
            ACCOUNT_VISIBILITY_CONFIGURATION configuration setting and the
            visibility preference of the user for whom data is requested.

            Note that a user can view which account fields they have shared
            with other users by requesting their own username and providing
            the "view=shared" URL parameter.

        **Response Values for PATCH**

            Users can only modify their own account information. If the
            requesting user does not have the specified username and has staff
            access, the request returns an HTTP 403 "Forbidden" response. If
            the requesting user does not have staff access, the request
            returns an HTTP 404 "Not Found" response to avoid revealing the
            existence of the account.

            If no user exists with the specified username, an HTTP 404 "Not
            Found" response is returned.

            If "application/merge-patch+json" is not the specified content
            type, a 415 "Unsupported Media Type" response is returned.

            If validation errors prevent the update, this method returns a 400
            "Bad Request" response that includes a "field_errors" field that
            lists all error messages.

            If a failure at the time of the update prevents the update, a 400
            "Bad Request" error is returned. The JSON collection contains
            specific errors.

            If the update is successful, updated user account data is returned.
    """
    authentication_classes = (
        OAuth2AuthenticationAllowInactiveUser, SessionAuthenticationAllowInactiveUser, JwtAuthentication
    )
    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (MergePatchParser,)

    def get(self, request):
        """
        GET /api/user/v1/me
        """
        return Response({'username': request.user.username})

    def list(self, request):
        """
        GET /api/user/v1/accounts?username={username1,username2}
        """
        usernames = request.GET.get('username')
        try:
            if usernames:
                usernames = usernames.strip(',').split(',')
            account_settings = get_account_settings(
                request, usernames, view=request.query_params.get('view'))
        except UserNotFound:
            return Response(status=status.HTTP_403_FORBIDDEN if request.user.is_staff else status.HTTP_404_NOT_FOUND)

        return Response(account_settings)

    def retrieve(self, request, username):
        """
        GET /api/user/v1/accounts/{username}/
        """
        try:
            account_settings = get_account_settings(
                request, [username], view=request.query_params.get('view'))
        except UserNotFound:
            return Response(status=status.HTTP_403_FORBIDDEN if request.user.is_staff else status.HTTP_404_NOT_FOUND)

        return Response(account_settings[0])

    def partial_update(self, request, username):
        """
        PATCH /api/user/v1/accounts/{username}/

        Note that this implementation is the "merge patch" implementation proposed in
        https://tools.ietf.org/html/rfc7396. The content_type must be "application/merge-patch+json" or
        else an error response with status code 415 will be returned.
        """
        try:
            with transaction.atomic():
                update_account_settings(request.user, request.data, username=username)
                account_settings = get_account_settings(request, [username])[0]
        except UserNotAuthorized:
            return Response(status=status.HTTP_403_FORBIDDEN if request.user.is_staff else status.HTTP_404_NOT_FOUND)
        except UserNotFound:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except AccountValidationError as err:
            return Response({"field_errors": err.field_errors}, status=status.HTTP_400_BAD_REQUEST)
        except AccountUpdateError as err:
            return Response(
                {
                    "developer_message": err.developer_message,
                    "user_message": err.user_message
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(account_settings)


class AccountDeactivationView(APIView):
    """
    Account deactivation viewset. Currently only supports POST requests.
    Only admins can deactivate accounts.
    """
    authentication_classes = (JwtAuthentication, )
    permission_classes = (permissions.IsAuthenticated, CanDeactivateUser)

    def post(self, request, username):
        """
        POST /api/user/v1/accounts/{username}/deactivate/

        Marks the user as having no password set for deactivation purposes.
        """
        _set_unusable_password(User.objects.get(username=username))
        return Response(get_account_settings(request, [username])[0])


class AccountRetireMailingsView(APIView):
    """
    Part of the retirement API, accepts POSTs to unsubscribe a user
    from all email lists.
    """
    authentication_classes = (JwtAuthentication, )
    permission_classes = (permissions.IsAuthenticated, CanRetireUser)

    def post(self, request, username):
        """
        POST /api/user/v1/accounts/{username}/retire_mailings/

        Allows an administrative user to take the following actions
        on behalf of an LMS user:
        -  Update UserOrgTags to opt the user out of org emails
        -  Call Sailthru API to force opt-out the user from all email lists
        """
        user_model = get_user_model()
        retired_username = request.data['retired_username']

        try:
            user = get_potentially_retired_user_by_username_and_hash(username, retired_username)

            with transaction.atomic():
                # Take care of org emails first, using the existing API for consistency
                for preference in UserOrgTag.objects.filter(user=user, key='email-optin'):
                    update_email_opt_in(user, preference.org, False)

                # This signal allows lms' email_marketing and other 3rd party email
                # providers to unsubscribe the user as well
                USER_RETIRE_MAILINGS.send(sender=self.__class__, user=user)
        except user_model.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return Response(text_type(exc), status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(status=status.HTTP_204_NO_CONTENT)


class DeactivateLogoutView(APIView):
    """
    POST /api/user/v1/accounts/deactivate_logout/
    {
        "user": "example_username",
    }

    **POST Parameters**

      A POST request must include the following parameter.

      * user: Required. The username of the user being deactivated.

    **POST Response Values**

     If the request does not specify a username or submits a username
     for a non-existent user, the request returns an HTTP 404 "Not Found"
     response.

     If a user who is not a superuser tries to deactivate a user,
     the request returns an HTTP 403 "Forbidden" response.

     If the specified user is successfully deactivated, the request
     returns an HTTP 204 "No Content" response.

     If an unanticipated error occurs, the request returns an
     HTTP 500 "Internal Server Error" response.

    Allows an administrative user to take the following actions
    on behalf of an LMS user:
    -  Change the user's password permanently to Django's unusable password
    -  Log the user out
    """
    authentication_classes = (JwtAuthentication, )
    permission_classes = (permissions.IsAuthenticated, CanRetireUser)

    def post(self, request):
        """
        POST /api/user/v1/accounts/deactivate_logout

        Marks the user as having no password set for deactivation purposes,
        and logs the user out.
        """
        username = request.data.get('user', None)
        if not username:
            return Response(
                status=status.HTTP_404_NOT_FOUND,
                data={
                    'message': u'The user was not specified.'
                }
            )

        user_model = get_user_model()
        try:
            # make sure the specified user exists
            user = user_model.objects.get(username=username)

            with transaction.atomic():
                # 1. Unlink LMS social auth accounts
                UserSocialAuth.objects.filter(user_id=user.id).delete()
                # 2. Change LMS password & email
                user.email = get_retired_email_by_email(user.email)
                user.save()
                _set_unusable_password(user)
                # 3. Unlink social accounts & change password on each IDA, still to be implemented
        except user_model.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:  # pylint: disable=broad-except
            return Response(text_type(exc), status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(status=status.HTTP_204_NO_CONTENT)


def _set_unusable_password(user):
    """
    Helper method for the shared functionality of setting a user's
    password to the unusable password, thus deactivating the account.
    """
    user.set_unusable_password()
    user.save()
