#!/usr/bin/env python3
"""
Test runner script for FAIR Signposting Crawler.
This script runs both unit tests and integration tests.
"""

import unittest
import sys
import os
import argparse

# Add the parent directory to the path so we can import modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def run_tests(test_type='all', verbose=False):
    """
    Run the specified tests.
    
    Args:
        test_type (str): Type of tests to run - 'unit', 'integration', or 'all'
        verbose (bool): Whether to show verbose output
    
    Returns:
        bool: True if all tests passed, False otherwise
    """
    # Create test loader
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Determine which test modules to load
    if test_type == 'unit' or test_type == 'all':
        print("Loading unit tests...")
        try:
            # Import the unit test module
            from tests import unit_tests_app
            
            # Add all test cases from the unit tests module
            for name in dir(unit_tests_app):
                obj = getattr(unit_tests_app, name)
                # Add only classes that are subclasses of TestCase but not TestCase itself
                if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj != unittest.TestCase:
                    tests = loader.loadTestsFromTestCase(obj)
                    suite.addTests(tests)
                    print(f"  Added tests from {name}")
        except ImportError as e:
            print(f"Error importing unit tests: {e}")
            if verbose:
                print(f"Make sure the file 'tests/unit_tests_app.py' exists and is properly formatted.")
        
    if test_type == 'integration' or test_type == 'all':
        print("Loading integration tests...")
        try:
            # Import the integration test module
            from tests import integration_tests_app
            
            # Add all test cases from the integration tests module
            for name in dir(integration_tests_app):
                obj = getattr(integration_tests_app, name)
                # Add only classes that are subclasses of TestCase but not TestCase itself
                if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj != unittest.TestCase:
                    tests = loader.loadTestsFromTestCase(obj)
                    suite.addTests(tests)
                    print(f"  Added tests from {name}")
        except ImportError as e:
            print(f"Error importing integration tests: {e}")
            if verbose:
                print(f"Make sure the file 'tests/integration_tests_app.py' exists and is properly formatted.")
    
    # Run the tests
    verbosity = 2 if verbose else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    
    # Return True if successful, False otherwise
    return result.wasSuccessful()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run tests for FAIR Signposting Crawler')
    parser.add_argument('--type', choices=['unit', 'integration', 'all'], default='all',
                        help='Type of tests to run (default: all)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show verbose output')
    
    args = parser.parse_args()
    
    success = run_tests(args.type, args.verbose)
    
    # Exit with appropriate status code
    sys.exit(0 if success else 1)