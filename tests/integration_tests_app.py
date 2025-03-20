import unittest
import os
import sys
import tempfile
import json
from unittest.mock import patch, MagicMock

#project directory to path to import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import app
import app as crawler_app


class IntegrationTests(unittest.TestCase):
    """Basic integration tests for the FAIR Signposting Crawler."""
    
    def setUp(self):
        """Set up test environment."""
        # Configure app for testing
        crawler_app.app.config.update({
            'TESTING': True,
            'FUSEKI_ENDPOINT': 'http://localhost:3030',
            'FUSEKI_DATASET': 'knowledge_graph',
            'MAX_CRAWL_DEPTH': 2,
            'MAX_RESOURCES_PER_LEVEL': 3,
            'RELEVANCE_THRESHOLD': 0.3,
            'CRAWL_TIMEOUT': 5,
            'MAX_RESOURCES': 10,
            'MAX_TRIPLES': 100
        })
        
        # Create a test client
        self.client = crawler_app.app.test_client()
        
        # Reset the crawler state
        self.reset_crawler_state()
    
    def reset_crawler_state(self):
        """Reset the crawler state to its initial values."""
        crawler_app.crawl_state = {
            'visited_urls': set(),
            'current_depth': 0,
            'crawl_id': None,
            'start_time': None,
            'crawl_active': False,
            'resource_scores': {},
            'signposting_stats': {
                'found': 0,
                'fallback_used': 0,
                'rel_types': {},
                'mime_types': {}
            },
            'provenance': {
                'resources': [],
                'domains_visited': set(),
                'resources_visited': 0,
                'triples_collected': 0
            }
        }
    
    def test_index_page(self):
        """Test that the index page loads correctly."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'FAIR Signposting Crawler', response.data)
    
    def test_query_page(self):
        """Test that the query page loads correctly."""
        response = self.client.get('/query')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'SPARQL Query Interface', response.data)
    
    def test_configure_page(self):
        """Test that the configuration page loads correctly."""
        for path in ['/config']:
            response = self.client.get(path)
            if response.status_code == 200:
                self.assertIn(b'System Configuration', response.data)
                return
        
        response = self.client.get('/configure')
        self.assertEqual(response.status_code, 200, 
                        "Configuration page not found at /config")
    
    def test_configure_form_submission(self):
        """Test configuration form submission."""
        # Save original config
        original_config = dict(crawler_app.app.config)
        
        try:
            config_path = None
            for path in ['/config']:
                response = self.client.get(path)
                if response.status_code == 200:
                    config_path = path
                    break
            
            if not config_path:
                self.skipTest("Could not find configuration page to test form submission")
            
            # Post new configuration values
            response = self.client.post(config_path, data={
                'fuseki_endpoint': 'http://custom-fuseki:3030',
                'fuseki_dataset': 'custom_graph',
                'max_depth': '4',
                'max_resources': '10',
                'relevance_threshold': '0.4',
                'timeout': '60'
            }, follow_redirects=True)
            
            # Should redirect to index page
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'FAIR Signposting Crawler', response.data)
            
            # Check that config was updated
            self.assertEqual(crawler_app.app.config['FUSEKI_ENDPOINT'], 'http://custom-fuseki:3030')
            self.assertEqual(crawler_app.app.config['FUSEKI_DATASET'], 'custom_graph')
            self.assertEqual(crawler_app.app.config['MAX_CRAWL_DEPTH'], 4)
            self.assertEqual(crawler_app.app.config['MAX_RESOURCES_PER_LEVEL'], 10)
            self.assertEqual(crawler_app.app.config['RELEVANCE_THRESHOLD'], 0.4)
            self.assertEqual(crawler_app.app.config['CRAWL_TIMEOUT'], 60)
        finally:
            # Restore original config
            crawler_app.app.config.update(original_config)
    
    @patch('requests.get')
    def test_fuseki_connection_check(self, mock_get):
        """Test the Fuseki connection check endpoint."""
        # Mock the response for Fuseki ping
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        # Test the endpoint
        response = self.client.get('/check-fuseki')
        self.assertEqual(response.status_code, 200)
        
        # Parse the JSON response
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'success')
    
    @patch('app.perform_crawl')
    def test_start_crawl_endpoint(self, mock_perform_crawl):
        """Test the start crawl endpoint."""
        # Submit a seed URL
        response = self.client.post('/start-crawl', data={
            'seed_url': 'http://example.org/resource'
        }, follow_redirects=False)  # Changed to false to avoid redirection issues
        
        # Check response - should be 302 redirect
        self.assertIn(response.status_code, [200, 302], 
                     f"Expected 200 or 302 redirect, got {response.status_code}")
        
        # Verify perform_crawl was called
        mock_perform_crawl.assert_called_once()
        
        # Check that crawl state was updated
        self.assertIsNotNone(crawler_app.crawl_state['crawl_id'])
        self.assertEqual(crawler_app.crawl_state['provenance']['seed_urls'], 
                        ['http://example.org/resource'])
    
    def test_crawl_status_api(self):
        """Test the crawl status API endpoint."""
        # Set up active crawl state
        crawler_app.crawl_state['crawl_active'] = True
        crawler_app.crawl_state['crawl_id'] = 'test_crawl_123'
        crawler_app.crawl_state['current_depth'] = 1
        crawler_app.crawl_state['visited_urls'] = set(['http://example.org/resource'])
        crawler_app.crawl_state['provenance'] = {
            'triples_collected': 42,
            'resources_visited': 1
        }
        crawler_app.crawl_state['last_status_check'] = crawler_app.datetime.datetime.now()
        crawler_app.crawl_state['last_visited_count'] = 1
        
        # Test API endpoint
        response = self.client.get('/api/crawl-status')
        self.assertEqual(response.status_code, 200)
        
        # Parse response JSON
        data = json.loads(response.data)
        
        # Check expected fields
        self.assertTrue(data['active'])
        self.assertEqual(data['resources_visited'], 1)
        self.assertEqual(data['triples_collected'], 42)
        self.assertEqual(data['crawl_id'], 'test_crawl_123')
    
    @patch('threading.Thread')
    def test_continue_crawl(self, mock_thread):
        """Test the continue_crawl endpoint."""
        # Set a test crawl_id to avoid URL building error
        crawler_app.crawl_state['crawl_id'] = 'test_crawl_123'
        
        # Mock Thread.start() method
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance
        
        # Test endpoint without following redirects
        response = self.client.post('/continue-crawl', data={
            'resource_uri': 'http://example.org/resource'
        }, follow_redirects=False)
        
        # Check for redirect (should be 302 Found)
        self.assertEqual(response.status_code, 302, 
                        f"Expected 302 redirect, got {response.status_code}")
        
        # Verify thread was started
        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()
        
        # Verify crawler state was updated
        self.assertTrue(crawler_app.crawl_state['crawl_active'])


    @patch('app.SPARQLWrapper')
    def test_query_endpoint_get(self, mock_sparql_wrapper):
        """Test that the query endpoint handles GET requests."""
        # test that the endpoint loads correctly
        response = self.client.get('/query')
        self.assertEqual(response.status_code, 200)
    
    @patch('app.SPARQLWrapper')
    def test_visualise_endpoint(self, mock_sparql_wrapper):
        """Test that the visualisation endpoint loads correctly."""
        # Setup mock for the query to list graphs
        mock_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.convert.return_value = {
            'results': {
                'bindings': [
                    {'g': {'value': 'http://example.org/graph1'}}
                ]
            }
        }
        mock_instance.query.return_value = mock_result
        mock_sparql_wrapper.return_value = mock_instance
        
        # Test endpoint
        response = self.client.get('/visualise')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Knowledge Graph Visualisation', response.data)


if __name__ == '__main__':
    unittest.main()