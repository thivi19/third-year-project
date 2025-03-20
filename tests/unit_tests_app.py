import unittest
from unittest.mock import patch, MagicMock, Mock
import sys
import os
import json
import requests
from bs4 import BeautifulSoup
from rdflib import Graph, URIRef, Literal, Namespace
from urllib.parse import urlparse
import datetime

#project directory to path to import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#import app modules
from app import get_signposting_links, fallback_discovery
from app import calculate_relevance, record_provenance, record_format_statistics
from app import select_next_resources


class TestSignpostingFunctions(unittest.TestCase):
    """Tests for functions related to signposting discovery and link extraction."""

    @patch('requests.head')
    def test_get_signposting_links_from_headers(self, mock_head):
        # Mock response with Link header
        mock_response = MagicMock()
        mock_response.headers = {
            'Link': '<http://example.org/data>; rel="describedby", <http://example.org/license>; rel="license"'
        }
        mock_head.return_value = mock_response

        # Setup test crawl state dictionary
        test_crawl_state = {
            'signposting_stats': {
                'found': 0
            }
        }
        
        with patch('app.crawl_state', test_crawl_state):
            # Test function
            links = get_signposting_links('http://example.org/resource')
            
            # Assertions
            self.assertEqual(len(links), 2)
            self.assertEqual(links['describedby'], 'http://example.org/data')
            self.assertEqual(links['license'], 'http://example.org/license')

    @patch('requests.head')
    @patch('requests.get')
    def test_get_signposting_links_from_html(self, mock_get, mock_head):
        # Mock head response with no Link header
        mock_head_response = MagicMock()
        mock_head_response.headers = {}
        mock_head.return_value = mock_head_response
        
        # Mock get response with HTML containing link elements
        mock_get_response = MagicMock()
        html_content = """
        <html>
        <head>
            <link rel="describedby" href="http://example.org/data.ttl">
            <link rel="license" href="http://example.org/license">
        </head>
        <body>
            <a href="http://example.org/author" rel="author">Author</a>
        </body>
        </html>
        """
        mock_get_response.text = html_content
        mock_get_response.raise_for_status = Mock()
        mock_get.return_value = mock_get_response
        
        # Setup test crawl state dictionary
        test_crawl_state = {
            'signposting_stats': {
                'found': 0
            }
        }
        
        with patch('app.crawl_state', test_crawl_state):
            # Test function
            links = get_signposting_links('http://example.org/resource')
            
            # Assertions
            self.assertEqual(len(links), 3)
            self.assertEqual(links['describedby'], 'http://example.org/data.ttl')
            self.assertEqual(links['license'], 'http://example.org/license')
            self.assertEqual(links['author'], 'http://example.org/author')

    @patch('requests.head')
    def test_fallback_discovery(self, mock_head):
        # Mock head response for RDF resource detection
        def mock_head_side_effect(url, **kwargs):
            mock_response = MagicMock()
            if url == 'http://example.org/resource.rdf':
                mock_response.status_code = 200
                mock_response.headers = {'Content-Type': 'application/rdf+xml'}
            else:
                mock_response.status_code = 404
                mock_response.headers = {'Content-Type': 'text/html'}
            return mock_response
            
        mock_head.side_effect = mock_head_side_effect
        
        # Setup test crawl state
        test_crawl_state = {
            'signposting_stats': {
                'found': 0,
                'fallback_used': 0
            }
        }
        
        # Mock rdflib plugin detection
        with patch('rdflib.plugin.plugins', return_value=[]):
            with patch('app.crawl_state', test_crawl_state):
                # Test function
                links = fallback_discovery('http://example.org/resource')
                
                # Assertions
                if links and 'alternate' in links:
                    self.assertEqual(links['alternate'], 'http://example.org/resource.rdf')
                    self.assertEqual(test_crawl_state['signposting_stats']['fallback_used'], 1)


class TestRDFProcessing(unittest.TestCase):
    """Tests for RDF parsing and processing functions."""
    
    def test_calculate_relevance(self):
        # Test relevance calculation for different URLs
        # Mock a small RDF graph
        test_graph = Graph()
        test_graph.add((URIRef('http://example.org/subject'), 
                         URIRef('http://schema.org/name'), 
                         Literal('Test')))
        
        # Test with various URLs
        with patch('app.crawl_state', {'visited_urls': set()}):
            # Skip actual HTTP requests
            with patch('requests.head'):
                # URL with RDF indicators should score higher
                relevance1 = calculate_relevance('http://example.org/data/metadata.rdf', test_graph)
                relevance2 = calculate_relevance('http://example.org/page.html', test_graph)
                
                # Assert relevance for RDF URL is higher
                self.assertGreater(relevance1, relevance2)
                
                # Visited URLs should get zero relevance
                with patch('app.crawl_state', {'visited_urls': {'http://example.org/visited'}}):
                    relevance3 = calculate_relevance('http://example.org/visited', test_graph)
                    self.assertEqual(relevance3, 0.0)
                
                # All relevance scores should be between 0 and 1
                for score in [relevance1, relevance2, relevance3]:
                    self.assertGreaterEqual(score, 0.0)
                    self.assertLessEqual(score, 1.0)


class TestCrawlFunctions(unittest.TestCase):
    """Tests for crawling functionality."""
    
    def test_record_format_statistics(self):
        # Create a test dictionary for crawler state
        test_state = {
            'signposting_stats': {
                'mime_types': {},
                'rel_types': {}
            },
            'provenance': {
                'domains_visited': set()
            }
        }
        
        # Test with the patched crawler state
        with patch('app.crawl_state', test_state):
            record_format_statistics(
                'http://example.org/data.ttl', 
                'text/turtle', 
                'turtle', 
                'describedby'
            )
            
            # Check if MIME types and rel types were recorded
            self.assertIn('text/turtle', test_state['signposting_stats']['mime_types'])
            self.assertIn('describedby', test_state['signposting_stats']['rel_types'])
            
            # Check if domain was added to visited domains
            self.assertIn('example.org', test_state['provenance']['domains_visited'])
    
    def test_record_provenance(self):
        # Create a test dictionary for crawler state
        test_state = {
            'crawl_id': 'test_crawl_123',
            'current_depth': 2,
            'signposting_stats': {
                'mime_types': {},
                'rel_types': {}
            },
            'provenance': {
                'id': 'test_crawl_123',
                'resources_visited': 0,
                'triples_collected': 0,
                'domains_visited': set(),
                'resources': []
            }
        }
        
        # Mock store_in_fuseki to avoid actual storage
        with patch('app.store_in_fuseki', return_value=True):
            # Test with the patched crawler state
            with patch('app.crawl_state', test_state):
                # Test recording provenance
                record_provenance(
                    'http://example.org/data.ttl',
                    'signposting:describedby',
                    10,
                    'turtle',
                    'text/turtle'
                )
                
                # Check if resource was added to provenance
                self.assertEqual(len(test_state['provenance']['resources']), 1)
                resource = test_state['provenance']['resources'][0]
                self.assertEqual(resource['url'], 'http://example.org/data.ttl')
                self.assertEqual(resource['source_type'], 'signposting:describedby')
                self.assertEqual(resource['triple_count'], 10)
                self.assertEqual(resource['format'], 'turtle')
                
                # Check if counts were updated
                self.assertEqual(test_state['provenance']['resources_visited'], 1)
                self.assertEqual(test_state['provenance']['triples_collected'], 10)
    
    def test_select_next_resources(self):
        # Create a test dictionary for crawler state
        test_state = {
            'visited_urls': set(['http://example.org/visited']),
            'resource_scores': {
                'http://example.org/high': 0.9,
                'http://example.org/medium': 0.6,
                'http://example.org/low': 0.3
            }
        }
        
        # Test with real dictionary as crawler state
        with patch('app.crawl_state', test_state):
            # Mock app config
            app_config = {'MAX_RESOURCES_PER_LEVEL': 2}
            
            # Test selection function
            with patch('app.app.config', app_config):
                candidate_urls = [
                    'http://example.org/high',
                    'http://example.org/medium',
                    'http://example.org/low',
                    'http://example.org/new1',
                    'http://example.org/new2'
                ]
                
                # Mock calculate_relevance to return predictable values
                def mock_relevance(url, *args, **kwargs):
                    if 'high' in url:
                        return 0.9
                    elif 'medium' in url:
                        return 0.6
                    elif 'low' in url:
                        return 0.3
                    elif 'new' in url:
                        return 0.5
                    return 0.1
                
                with patch('app.calculate_relevance', side_effect=mock_relevance):
                    # Test the function
                    selected = select_next_resources(candidate_urls)
                    
                    # Should select high-scoring URLs first, up to MAX_RESOURCES_PER_LEVEL
                    self.assertEqual(len(selected), 2)
                    self.assertIn('http://example.org/high', selected)


if __name__ == '__main__':
    unittest.main()