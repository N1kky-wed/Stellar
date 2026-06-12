import pytest
from unittest.mock import patch, MagicMock
import requests
from webscrapper import scrape_url

# Tests for the webscrapper module to ensure robust URL content extraction and security error handling.

@patch('requests.get')
def test_scrape_url_success_cleans_content(mock_get):
    """
    Asserts that a successful GET request parses paragraphs, strips non-alphanumeric characters,
    consolidates whitespace, and returns the cleaned text.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><head><title> Test! Title </title></head><body><p>Hello,   World!</p><p>Python 123.</p></body></html>'
    mock_get.return_value = mock_resp

    result = scrape_url('http://example.com')
    assert "context from http://example.com: is" in result
    assert "Hello World Python 123" in result


@patch('requests.get')
def test_scrape_url_redirect_returns_security_error(mock_get):
    """
    Asserts that redirecting status codes are explicitly blocked for SSRF protection and security.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 301
    mock_resp.headers = {'Location': 'http://internal-malicious.local'}
    mock_get.return_value = mock_resp

    result = scrape_url('http://example.com')
    assert "Redirects are not allowed" in result
    assert "http://internal-malicious.local" in result


@patch('requests.get')
def test_scrape_url_timeout_returns_timeout_message(mock_get):
    """
    Asserts that request timeout exceptions are caught and return a user-friendly timeout message.
    """
    mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

    result = scrape_url('http://example.com')
    assert "took too long and timed out" in result


@patch('requests.get')
def test_scrape_url_request_exception_returns_error_message(mock_get):
    """
    Asserts that requests exceptions (connection, DNS, etc.) are handled gracefully and logged.
    """
    mock_get.side_effect = requests.exceptions.RequestException("DNS lookup failed")

    result = scrape_url('http://example.com')
    assert "An error occurred while fetching the URL" in result
    assert "DNS lookup failed" in result


@patch('requests.get')
def test_scrape_url_unexpected_exception_returns_generic_error(mock_get):
    """
    Asserts that general/unexpected python runtime exceptions are caught and return a generic error.
    """
    mock_get.side_effect = Exception("Unexpected memory failure")

    result = scrape_url('http://example.com')
    assert "An unexpected error occurred" in result
    assert "Unexpected memory failure" in result
