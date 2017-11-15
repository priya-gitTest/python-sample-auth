"""sample Microsoft Graph authentication library"""
# Copyright (c) Microsoft. All rights reserved. Licensed under the MIT license.
# See LICENSE in the project root for license information.
import json
import os
import time
import urllib.parse
import urllib3
import uuid

import requests
import bottle

import config

# disable warnings to allow use of non-HTTPS for local dev/test
urllib3.disable_warnings()

class GraphSession(object):
    """Microsoft Graph connection class. Implements OAuth 2.0 Authorization Code
    Grant workflow, handles configuration and state management, adding tokens
    for authenticated calls to Graph, related details.
    """

    def __init__(self, **kwargs):
        """Initialize instance with default values and user-provided overrides.

        These settings must be specified at runtime:
        client_id = client ID (application ID) from app registration portal
        client_secret = client secret (password) from app registration portal
        redirect_uri = must match value specified in app registration portal
        scopes = list of required scopes/permissions

        These settings have default values but can be overridden if desired:
        resource = the base URL for calls to Microsoft Graph
        api_version = Graph version ('v1.0' is default, can also use 'beta')
        authority_url = base URL for authorization authority
        auth_endpoint = authentication endpoint (at authority_url)
        token_endpoint = token endpoint (at authority_url)
        cache_state = whether to cache session state in local state.json file
                      If cache_state==True and a valid access token has been
                      cached, the token will be used without any user
                      authentication required ("silent SSO")
        refresh_enable = whether to auto-refresh expired tokens
        """

        self.config = {'client_id': '', 'client_secret': '', 'redirect_uri': '',
                       'scopes': [], 'cache_state': False,
                       'resource': config.RESOURCE, 'api_version': config.API_VERSION,
                       'authority_url': config.AUTHORITY_URL,
                       'auth_endpoint': config.AUTHORITY_URL + config.AUTH_ENDPOINT,
                       'token_endpoint': config.AUTHORITY_URL + config.TOKEN_ENDPOINT,
                       'refresh_enable': True}

        # Print warning if any unknown arguments were passed, since those may be
        # errors/typos.
        for key in kwargs:
            if not key in self.config:
                print(f'WARNING: unknown "{key}" argument passed to GraphSession')

        self.config.update(kwargs.items()) # add passed arguments to config

        self.state_manager('init')

        # used by login() and redirect_uri_handler() to identify current session
        self.authstate = ''

        # route to redirect to after authentication; can be overridden in login()
        self.login_redirect = '/'

        # If refresh tokens are enabled, add the offline_access scope.
        # Note that refresh_enable setting takes precedence over whether
        # the offline_access scope is explicitly requested.
        refresh_scope = 'offline_access'
        if self.config['refresh_enable']:
            if refresh_scope not in self.config['scopes']:
                self.config['scopes'].append(refresh_scope)
        else:
            if refresh_scope in self.config['scopes']:
                self.config['scopes'].remove(refresh_scope)

    def __repr__(self):
        """Return string representation of class instance."""
        return ('<GraphSession(loggedin='
                f'{"True" if self.state["loggedin"] else "False"}'
                f', client_id={self.config["client_id"]})>')

    def api_endpoint(self, url):
        """Convert relative endpoint (e.g., 'me') to full Graph API endpoint."""

        if urllib.parse.urlparse(url).scheme in ['http', 'https']:
            return url
        return urllib.parse.urljoin(
            f"{self.config['resource']}{self.config['api_version']}/",
            url.lstrip('/'))

    def headers(self, headers=None):
        """Returns dictionary of default HTTP headers for calls to Microsoft Graph API,
        including access token and a unique client-request-id.

        Keyword arguments:
        headers -- optional additional headers or overrides for default headers
        """

        token = self.state['access_token']
        merged_headers = {'User-Agent' : 'graphrest-python',
                          'Authorization' : f'Bearer {token}',
                          'Accept' : 'application/json',
                          'Content-Type' : 'application/json',
                          'SdkVersion': 'sample-python-graphrest',
                          'x-client-sku': 'sample-python-graphrest',
                          'client-request-id' : str(uuid.uuid4()),
                          'return-client-request-id' : 'true'}
        if headers:
            merged_headers.update(headers)
        return merged_headers

    def login(self, login_redirect=None):
        """Ask user to authenticate via Azure Active Directory.
        Optional login_redirect argument is route to redirect to after user
        is authenticated.
        """
        if login_redirect:
            self.login_redirect = login_redirect

        # if caching is enabled, attempt silent SSO first
        if self.config['cache_state']:
            if self.silent_sso():
                return bottle.redirect(self.login_redirect)

        self.authstate = str(uuid.uuid4())
        params = urllib.parse.urlencode({'response_type': 'code',
                                         'client_id': self.config['client_id'],
                                         'redirect_uri': self.config['redirect_uri'],
                                         'scope': ' '.join(self.config['scopes']),
                                         'state': self.authstate,
                                         'prompt': 'select_account'})
        self.state['authorization_url'] = self.config['auth_endpoint'] + '?' + params
        bottle.redirect(self.state['authorization_url'], 302)

    def logout(self, redirect_to=None):
        """Clear current Graph connection state and redirect to specified route.
        If redirect_to == None, no redirection will take place and just clears
        the current logged-in status.
        """

        self.state_manager('init')
        if redirect_to:
            bottle.redirect(redirect_to)

    def redirect_uri_handler(self):
        """Redirect URL handler for AuthCode workflow. Uses the authorization
        code received from auth endpoint to call the token endpoint and obtain
        an access token.
        """

        # Verify that this authorization attempt came from this app, by checking
        # the received state against what we sent with our authorization request.
        if self.authstate != bottle.request.query.state:
            raise Exception(f"STATE MISMATCH: {self.authstate} sent,"
                            f"{bottle.request.query.state} received")
        self.authstate = '' # clear state to prevent re-use

        token_response = requests.post(self.config['token_endpoint'],
                                       data={'client_id': self.config['client_id'],
                                             'client_secret': self.config['client_secret'],
                                             'grant_type': 'authorization_code',
                                             'code': bottle.request.query.code,
                                             'redirect_uri': self.config['redirect_uri']})
        self.token_save(token_response)

        if token_response and token_response.ok:
            self.state_manager('save')
        return bottle.redirect(self.login_redirect)

    def silent_sso(self):
        """Attempt silent SSO, by checking whether current access token is valid
        and/or attempting to refresh it.

        Return True is we have successfully stored a valid access token.
        """
        if self.token_seconds() > 0:
            return True # current token is vald

        if self.state['refresh_token']:
            # we have a refresh token, so use it to refresh the access token
            self.token_refresh()
            return True

        return False # can't do silent SSO at this time

    def state_manager(self, action):
        """Manage self.state dictionary (session/connection metadata).

        action argument must be one of these:
        'init' -- initialize state (set properties to defaults)
        'save' -- save current state (if self.config['cache_state'])
        """

        initialized_state = {'access_token': None, 'refresh_token': None,
                             'token_expires_at': 0, 'authorization_url': '',
                             'token_scope': '', 'loggedin': False}
        filename = 'state.json'

        if action == 'init':
            self.state = initialized_state
            if self.config['cache_state'] and os.path.isfile(filename):
                with open(filename) as fhandle:
                    self.state.update(json.loads(fhandle.read()))
                self.token_validation()
            elif not self.config['cache_state'] and os.path.isfile(filename):
                os.remove(filename)
        elif action == 'save' and self.config['cache_state']:
            with open(filename, 'w') as fhandle:
                fhandle.write(json.dumps(
                    {key:self.state[key] for key in initialized_state}))

    def token_refresh(self):
        """Refresh the current access token."""
        response = requests.post(self.config['token_endpoint'],
                                 data={'client_id': self.config['client_id'],
                                       'client_secret': self.config['client_secret'],
                                       'grant_type': 'refresh_token',
                                       'refresh_token': self.state['refresh_token']},
                                 verify=False)
        self.token_save(response)

    def token_save(self, response):
        """Parse an access token out of the JWT response from token endpoint and save it.

        Arguments:
        response -- response object returned by self.config['token_endpoint'], which
                    contains a JSON web token

        Returns True if the token was successfully saved, False if not.
        To manually inspect the contents of a JWT, see http://jwt.ms/.
        """

        jsondata = response.json()
        if not 'access_token' in jsondata:
            self.logout()
            return False

        self.verify_scopes(jsondata['scope'])
        self.state['access_token'] = jsondata['access_token']
        self.state['loggedin'] = True

        # token_expires_at = time.time() value (seconds) at which it expires
        self.state['token_expires_at'] = time.time() + int(jsondata['expires_in'])
        self.state['refresh_token'] = jsondata.get('refresh_token', None)
        return True

    def token_seconds(self):
        """Return number of seconds until current access token will expire."""

        if not self.state['access_token'] or time.time() >= self.state['token_expires_at']:
            return 0
        return int(self.state['token_expires_at'] - time.time())

    def token_validation(self, nseconds=5):
        """Verify that current access token is valid for at least nseconds, and
        if not then attempt to refresh it. Can be used to assure a valid token
        before making a call to Graph.
        """

        if self.token_seconds() < nseconds and self.config['refresh_enable']:
            self.token_refresh()

    def verify_scopes(self, token_scopes):
        """Verify that the list of scopes returned with an access token match
        the scopes that we requested.
        """

        self.state['token_scope'] = token_scopes
        scopes_returned = frozenset({_.lower() for _ in token_scopes.split(' ')})
        scopes_expected = frozenset({_.lower() for _ in self.config['scopes']
                                     if _.lower() != 'offline_access'})
        if scopes_expected != scopes_returned:
            print(f'scopes {list(scopes_expected)} requested, but scopes '
                  f'{list(scopes_returned)} returned with token')
