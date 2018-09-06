#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys

try:
    from setuptools import setup
except ImportError:
    print('Please install or upgrade setuptools or pip to continue')
    sys.exit(1)


def read(filename):
    with open(filename, 'rb') as f:
        return f.read().decode('utf8')


long_description = '\n\n'.join([read('README'),
                                read('AUTHORS'),
                                read('CHANGES')])

__doc__ = long_description

requirements = ['pyvisa>=1.8']


setup(name='PyVISA-py',
      description='Python VISA bindings for GPIB, RS232, and USB instruments',
      version='0.3.0',
      long_description=long_description,
      author='Hernan E. Grecco',
      author_email='hernan.grecco@gmail.com',
      maintainer='Hernan E. Grecco',
      maintainer_email='hernan.grecco@gmail.com',
      url='https://github.com/pyvisa/pyvisa-py',
      test_suite='pyvisa-py.testsuite.testsuite',
      keywords='Remote VISA GPIB USB serial RS232 measurement acquisition',
      license='MIT License',
      install_requires=requirements,
      classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: MIT License',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Programming Language :: Python',
        'Topic :: Scientific/Engineering :: Interface Engine/Protocol Translator',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        ],
      packages=['pyvisa-py',
                'pyvisa-py.protocols',
                'pyvisa-py.testsuite'],
      platforms="Linux, Windows, Mac",
      use_2to3=False,
      zip_safe=False)
