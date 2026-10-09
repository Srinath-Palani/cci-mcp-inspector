"""
OAuth Manager

Handles OAuth 2.0/2.1 authentication flows for MCP server connections.
Supports Client Credentials, Authorization Code, and token refresh flows.
"""

import time
import json
import base64
import hashlib
import secrets
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import requests
from pathlib import Path


class OAuthManager:
    """
    Manages OAuth authentication for MCP server connections.
    Supports multiple OAuth flows and automatic token refresh.
    """
    
    def __init__(self, auth_config: Dict[str, Any], cache_dir: Optional[Path] = None):
        """
        Initialize OAuth manager.
        
        Args:
            auth_config: OAuth configuration dictionary
            cache_dir: Directory to cache tokens (optional)
        """
        self.auth_config = auth_config
        self.auth_type = auth_config.get("type", "").lower()
        
        # OAuth endpoints
        self.token_url = auth_config.get("token_url")
        self.authorize_url = auth_config.get("authorize_url")
        
        # Client credentials
        self.client_id = auth_config.get("client_id")
        self.client_secret = auth_config.get("client_secret")
        
        # Scopes
        self.scopes = auth_config.get("scopes", [])
        if isinstance(self.scopes, str):
            self.scopes = self.scopes.split()
        
        # Token cache
        self.cache_dir = cache_dir or Path.home() / ".mcp_inspector" / "oauth_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Current token - check if tokens are provided in config (e.g., from Gemini)
        self.access_token: Optional[str] = auth_config.get("access_token")
        self.refresh_token: Optional[str] = auth_config.get("refresh_token")
        
        # Parse expires_at if provided (can be timestamp in milliseconds or seconds)
        expires_at = auth_config.get("expires_at")
        if expires_at:
            try:
                # Handle both milliseconds and seconds timestamps
                if expires_at > 1e10:  # Likely milliseconds
                    expires_at = expires_at / 1000
                self.token_expires_at = datetime.fromtimestamp(expires_at)
            except (ValueError, TypeError):
                self.token_expires_at = None
        else:
            self.token_expires_at = None
    
    def get_access_token(self) -> str:
        """
        Get valid access token, refreshing if necessary.
        
        Returns:
            Valid access token
        
        Raises:
            ValueError: If OAuth configuration is invalid
            requests.HTTPError: If token request fails
        """
        # If we have an access_token from config and no expiration, use it directly
        if self.access_token and not self.token_expires_at:
            return self.access_token
        
        # Check if we have a valid cached token (from config or cache)
        if self.access_token and self.token_expires_at:
            # Check if token is still valid (with 5 minute buffer)
            if datetime.now() < self.token_expires_at - timedelta(minutes=5):
                return self.access_token
            # Token expired, but we have it - try to refresh if possible
            if self.refresh_token:
                try:
                    return self._refresh_access_token()
                except Exception as e:
                    # Refresh failed, but try using the token anyway (might still work)
                    print(f"⚠️  Token refresh failed: {e}, trying existing token...")
                    return self.access_token
        
        # Try to load from cache
        cached_token = self._load_cached_token()
        if cached_token:
            self.access_token = cached_token["access_token"]
            self.refresh_token = cached_token.get("refresh_token")
            self.token_expires_at = datetime.fromtimestamp(cached_token["expires_at"])
            
            if datetime.now() < self.token_expires_at - timedelta(minutes=5):
                return self.access_token
        
        # Token expired or not available, get new token
        if self.refresh_token:
            # Try refresh token first
            try:
                return self._refresh_access_token()
            except:
                pass  # Fall back to full auth flow
        
        # Get new token based on flow type
        if "client_credentials" in self.auth_type:
            return self._client_credentials_flow()
        elif "authorization_code" in self.auth_type:
            return self._authorization_code_flow()
        else:
            raise ValueError(f"Unsupported OAuth flow: {self.auth_type}")
    
    def _client_credentials_flow(self) -> str:
        """
        OAuth 2.1 Client Credentials flow (machine-to-machine).
        
        Returns:
            Access token
        """
        if not self.token_url or not self.client_id or not self.client_secret:
            raise ValueError("Client Credentials flow requires token_url, client_id, and client_secret")
        
        print(f"🔐 Requesting OAuth token via Client Credentials flow...")
        print(f"   Token URL: {self.token_url}")
        print(f"   Client ID: {self.client_id[:8]}...")
        
        # Prepare request
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        
        if self.scopes:
            data["scope"] = " ".join(self.scopes)
            print(f"   Scopes: {', '.join(self.scopes)}")
        
        # Request token
        try:
            response = requests.post(
                self.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            response.raise_for_status()
            
            token_data = response.json()
            
            # Extract token info
            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
            
            print(f"✅ Token obtained successfully")
            print(f"   Expires in: {expires_in} seconds ({expires_in//60} minutes)")
            
            # Cache token
            self._cache_token(token_data)
            
            return self.access_token
            
        except requests.HTTPError as e:
            print(f"❌ Failed to obtain token: {e}")
            if e.response:
                print(f"   Response: {e.response.text}")
            raise
    
    def _authorization_code_flow(self) -> str:
        """
        OAuth 2.1 Authorization Code flow (with PKCE).
        
        Returns:
            Access token
        """
        if not self.authorize_url or not self.token_url or not self.client_id:
            raise ValueError("Authorization Code flow requires authorize_url, token_url, and client_id")
        
        print(f"\n🔐 Starting OAuth Authorization Code flow...")
        print(f"   Authorization URL: {self.authorize_url}")
        
        # Generate PKCE challenge
        code_verifier, code_challenge = self._generate_pkce_challenge()
        
        # Build authorization URL
        auth_params = {
            "client_id": self.client_id,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": self.auth_config.get("redirect_uri", "http://localhost:8080/callback")
        }
        
        if self.scopes:
            auth_params["scope"] = " ".join(self.scopes)
        
        auth_url = f"{self.authorize_url}?{'&'.join(f'{k}={v}' for k, v in auth_params.items())}"
        
        print(f"\n📋 Please authorize this application:")
        print(f"   1. Open this URL in your browser:")
        print(f"      {auth_url}")
        print(f"   2. Complete the authorization")
        print(f"   3. Copy the authorization code from the redirect URL")
        
        # Get authorization code from user
        auth_code = input("\n   Enter authorization code: ").strip()
        
        # Exchange code for token
        token_data = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
            "redirect_uri": auth_params["redirect_uri"]
        }
        
        if self.client_secret:
            token_data["client_secret"] = self.client_secret
        
        print(f"\n🔄 Exchanging authorization code for access token...")
        
        try:
            response = requests.post(
                self.token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            response.raise_for_status()
            
            token_response = response.json()
            
            # Extract token info
            self.access_token = token_response["access_token"]
            self.refresh_token = token_response.get("refresh_token")
            expires_in = token_response.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
            
            print(f"✅ Token obtained successfully")
            print(f"   Expires in: {expires_in} seconds ({expires_in//60} minutes)")
            
            # Cache token
            self._cache_token(token_response)
            
            return self.access_token
            
        except requests.HTTPError as e:
            print(f"❌ Failed to exchange code: {e}")
            if e.response:
                print(f"   Response: {e.response.text}")
            raise
    
    def _refresh_access_token(self) -> str:
        """
        Refresh access token using refresh token.
        
        Returns:
            New access token
        """
        if not self.refresh_token:
            raise ValueError("No refresh token available")
        
        print(f"🔄 Refreshing access token...")
        
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id
        }
        
        if self.client_secret:
            data["client_secret"] = self.client_secret
        
        try:
            response = requests.post(
                self.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            response.raise_for_status()
            
            token_data = response.json()
            
            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token", self.refresh_token)
            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
            
            print(f"✅ Token refreshed successfully")
            
            # Cache new token
            self._cache_token(token_data)
            
            return self.access_token
            
        except requests.HTTPError as e:
            print(f"⚠️  Failed to refresh token: {e}")
            raise
    
    def _generate_pkce_challenge(self) -> Tuple[str, str]:
        """
        Generate PKCE code verifier and challenge.
        
        Returns:
            Tuple of (code_verifier, code_challenge)
        """
        # Generate code verifier (43-128 characters)
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
        
        # Generate code challenge (SHA256 hash of verifier)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode('utf-8')).digest()
        ).decode('utf-8').rstrip('=')
        
        return code_verifier, code_challenge
    
    def _cache_token(self, token_data: Dict[str, Any]):
        """
        Cache token to disk for reuse.
        
        Args:
            token_data: Token response from OAuth server
        """
        cache_file = self.cache_dir / f"{self.client_id}_token.json"
        
        cache_data = {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in", 3600),
            "expires_at": (datetime.now() + timedelta(seconds=token_data.get("expires_in", 3600))).timestamp(),
            "token_type": token_data.get("token_type", "Bearer"),
            "scope": token_data.get("scope"),
            "cached_at": datetime.now().isoformat()
        }
        
        try:
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
            cache_file.chmod(0o600)  # Secure permissions
        except Exception as e:
            print(f"⚠️  Failed to cache token: {e}")
    
    def _load_cached_token(self) -> Optional[Dict[str, Any]]:
        """
        Load cached token from disk.
        
        Returns:
            Cached token data or None if not available
        """
        cache_file = self.cache_dir / f"{self.client_id}_token.json"
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            
            # Check if token is still valid
            expires_at = cache_data.get("expires_at")
            if expires_at and datetime.fromtimestamp(expires_at) > datetime.now():
                print(f"📦 Using cached token (expires in {int((datetime.fromtimestamp(expires_at) - datetime.now()).total_seconds() / 60)} minutes)")
                return cache_data
            
        except Exception as e:
            print(f"⚠️  Failed to load cached token: {e}")
        
        return None
    
    def clear_cache(self):
        """Clear cached tokens."""
        cache_file = self.cache_dir / f"{self.client_id}_token.json"
        if cache_file.exists():
            cache_file.unlink()
            print(f"🗑️  Cleared cached token")


def get_oauth_token(auth_config: Dict[str, Any]) -> str:
    """
    Convenience function to get OAuth token.
    
    Args:
        auth_config: OAuth configuration dictionary
    
    Returns:
        Access token
    
    Example:
        >>> auth_config = {
        ...     "type": "oauth2_1_client_credentials",
        ...     "token_url": "https://api.example.com/oauth/token",
        ...     "client_id": "your-client-id",
        ...     "client_secret": "your-client-secret",
        ...     "scopes": ["read", "write"]
        ... }
        >>> token = get_oauth_token(auth_config)
        >>> print(f"Token: {token}")
    """
    manager = OAuthManager(auth_config)
    return manager.get_access_token()


if __name__ == "__main__":
    """
    Test OAuth manager with example configuration.
    """
    import sys
    
    print("OAuth Manager Test")
    print("="*60)
    
    # Example: Client Credentials flow
    test_config = {
        "type": "oauth2_1_client_credentials",
        "token_url": "https://api.example.com/oauth/token",
        "client_id": "your-client-id",
        "client_secret": "your-client-secret",
        "scopes": ["read", "write"]
    }
    
    print("\nExample configuration:")
    print(json.dumps(test_config, indent=2))
    
    print("\nTo test with your OAuth server:")
    print("1. Update test_config with your OAuth credentials")
    print("2. Run: python src/utility/oauth_manager.py")
    print("\nOr use in your code:")
    print("  from src.utility.oauth_manager import get_oauth_token")
    print("  token = get_oauth_token(config)")

