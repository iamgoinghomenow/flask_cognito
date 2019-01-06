from collections import OrderedDict
from functools import wraps
from flask import _request_ctx_stack, current_app, jsonify, request
from werkzeug.local import LocalProxy
from cognitojwt import CognitoJWTException, decode as cognito_jwt_decode
import logging

log = logging.getLogger(__name__)

CONFIG_DEFAULTS = {
    'COGNITO_CHECK_TOKEN_EXPIRATION': True,
    'COGNITO_JWT_HEADER_NAME': 'Authorization',
    'COGNITO_JWT_HEADER_PREFIX': 'Bearer',
}

# user from pool
cognito_user = LocalProxy(lambda: getattr(_request_ctx_stack.top, 'current_cognito_user', None))

# unused - could be a way to add mapping of cognito user to application user
cognito_identity = LocalProxy(lambda: getattr(_request_ctx_stack.top, 'current_cognito_identity', None))

# access initialized cognito extension
_cog = LocalProxy(lambda: current_app.extensions['cognito_auth'])


class CognitoAuthError(Exception):
    def __init__(self, error, description, status_code=401, headers=None):
        self.error = error
        self.description = description
        self.status_code = status_code
        self.headers = headers

    def __repr__(self):
        return f'CognitoAuthError: {self.error}'

    def __str__(self):
        return f'{self.error} {self.description}'


class CognitoAuth(object):
    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        for k, v in CONFIG_DEFAULTS.items():
            app.config.setdefault(k, v)

        # required configuration
        self.region = self._get_required_config(app, 'COGNITO_REGION')
        self.userpool_id = self._get_required_config(app, 'COGNITO_USERPOOL_ID')
        self.app_client_id = self._get_required_config(app, 'COGNITO_APP_CLIENT_ID')
        self.jwt_header_name = self._get_required_config(app, 'COGNITO_JWT_HEADER_NAME')
        self.jwt_header_prefix = self._get_required_config(app, 'COGNITO_JWT_HEADER_PREFIX')

        # optional configuration
        self.check_expiration = app.config.get('COGNITO_CHECK_TOKEN_EXPIRATION', True)

        # save for localproxy
        app.extensions['cognito_auth'] = self

        # handle CognitoJWTExceptions
        app.errorhandler(CognitoAuthError)(self._cognito_auth_error_handler)

    def _get_required_config(self, app, config_name):
        val = app.config.get(config_name)
        if not val:
            raise Exception(f"{config_name} not found in app configuration but it is required.")
        return val

    def get_token(self):
        """Get token from request."""
        auth_header_name = _cog.jwt_header_name
        auth_header_prefix = _cog.jwt_header_prefix

        # get token value from header
        auth_header_value = request.headers.get(auth_header_name)
        parts = auth_header_value.split()

        if parts[0].lower() != auth_header_prefix.lower():
            raise CognitoAuthError('Invalid Cognito JWT header', f'Unsupported authorization type. Header prefix "{parts[0].lower()}" does not match "{auth_header_prefix.lower()}"')
        elif len(parts) == 1:
            raise CognitoAuthError('Invalid Cognito JWT header', 'Token missing')
        elif len(parts) > 2:
            raise CognitoAuthError('Invalid Cognito JWT header', 'Token contains spaces')

        return parts[1]

    def get_cognito_user(self, payload):
        """Get descriptor of cognito user from JWT payload."""
        return payload

    def get_identity(self, cognito_user):
        """Get application user identity from Cognito user descriptor."""
        return None

    def _cognito_auth_error_handler(self, error):
        log.exception(error)
        return jsonify(OrderedDict([
            ('status_code', error.status_code),
            ('error', error.error),
            ('description', error.description),
        ])), error.status_code, error.headers


def cognito_auth_required(fn):
    """View decorator that requires a valid Cognito JWT token to be present in the request

    :param realm: an optional realm
    """
    @wraps(fn)
    def decorator(*args, **kwargs):
        _cognito_auth_required()
        return fn(*args, **kwargs)
    return decorator


def _cognito_auth_required():
    """Does the actual work of verifying the Cognito JWT data in the current request.
    This is done automatically for you by `cognito_jwt_required()` but you could call it manually.
    Doing so would be useful in the context of optional JWT access in your APIs.
    """
    token = _cog.get_token()

    if token is None:
        auth_header_name = _cog.jwt_header_name
        auth_header_prefix = _cog.jwt_header_prefix
        raise CognitoAuthError('Authorization Required', f'Request does not contain a well-formed access token in {auth_header_name} beginning with "{auth_header_prefix}"')

    try:
        payload = cognito_jwt_decode(
            token=token,
            region=_cog.region,
            app_client_id=_cog.app_client_id,
            userpool_id=_cog.userpool_id,
            testmode=not _cog.check_expiration,
        )
    except CognitoJWTException as e:
        log.exception(e)
        raise CognitoAuthError('Invalid Cognito Authentication Token', str(e))

    _request_ctx_stack.top.current_cognito_user = _cog.get_cognito_user(payload)
    _request_ctx_stack.top.current_cognito_identity = _cog.get_identity(payload)