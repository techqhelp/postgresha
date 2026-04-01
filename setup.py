from setuptools import setup, find_packages

setup(
    name            = "pgha",
    version         = "1.0.0",
    description     = "PostgreSQL HA Heartbeat Manager for GCP Regional Persistent Disk",
    author          = "pgha contributors",
    python_requires = ">=3.6",
    package_dir     = {"": "src"},
    packages        = find_packages(where="src"),
    install_requires= [
        "psutil>=5.9.0",
        "psycopg2-binary==2.9.5",
        "google-auth>=2.17.0",
        "requests>=2.28.0",
    ],
    scripts         = [
        "bin/pgha-daemon",
        "bin/pgha-ctl",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: POSIX :: Linux",
        "Topic :: Database",
        "Topic :: System :: Clustering",
    ],
)
