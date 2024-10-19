import unittest
from unittest.mock import patch, Mock, call
from signposting_adopter_checker import discover_signposting_links, fetch_linkset

class TestSignpostingFunctions(unittest.TestCase):

    @patch('requests.get')
    def test_discover_signposting_links_with_links(self, mock_get):
        # mock the response for a URL with 'link' headers
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {
            'link': '<http://example.com/linkset>; rel="linkset", <http://example.com/another>; rel="related"'
        }
        mock_get.return_value = mock_response

        # calling the function
        discover_signposting_links("http://example.com")

        # check if requests.get was called with the expected URLs
        expected_calls = [
            call("http://example.com"),
            call("http://example.com/linkset", headers={"Accept": "application/linkset+json"}),
            call("http://example.com/another"),
        ]
        mock_get.assert_has_calls(expected_calls, any_order=True)

    @patch('requests.get')
    def test_discover_signposting_links_no_links(self, mock_get): # mock the response for a URL with no 'link' headers
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {}  # no headers
        mock_get.return_value = mock_response

        discover_signposting_links("http://example.com")

        # check if requests.get was called with the expected URL
        mock_get.assert_called_once_with("http://example.com")

    @patch('requests.get')
    def test_discover_signposting_links_with_error(self, mock_get):
        # mock the response for a URL that returns an error
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.headers = {}  # no headers
        mock_get.return_value = mock_response

        discover_signposting_links("http://example.com")

        # check if requests.get was called with the expected URL
        mock_get.assert_called_once_with("http://example.com")

    @patch('requests.get')
    def test_fetch_linkset_success(self, mock_get):
        # mock the response for a linkset request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'linkset': []}  # mock the JSON response
        mock_get.return_value = mock_response

        # now call the fetch_linkset function
        result = fetch_linkset("http://example.com/linkset")

        # check if the result is as expected
        self.assertEqual(result, {'linkset': []})
        mock_get.assert_called_once_with("http://example.com/linkset", headers={"Accept": "application/linkset+json"})

    @patch('requests.get')
    def test_fetch_linkset_failure(self, mock_get):
        # mock the response for a linkset request that fails
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        # call the fetch_linkset function
        result = fetch_linkset("http://example.com/linkset")

        # check if the result is None
        self.assertIsNone(result)
        mock_get.assert_called_once_with("http://example.com/linkset", headers={"Accept": "application/linkset+json"})

if __name__ == '__main__':
    unittest.main()
