r"""Download artifacts from AppVeyor builds of the same commit/pull request.

This tool is mainly used to download a ".coverage" file from AppVeyor to
combine it with the one in Travis (since Coveralls doesn't support multi-ci
code coverage). However this can be used to download any artifact from an
AppVeyor project.

If your project creates multiple jobs for one commit (e.g. different Python
versions, or a matrix in either yaml file), you can use the `--job-name`
option to get artifacts matching your local environment. Example:
appveyor-artifacts --job-name="Environment: PYTHON=C:\Python27" download

TODO:
1) --mangle-coverage
9) Tox tests on travis should test real-world. Get these files and md5 compare.

https://github.com/Robpol86/appveyor-artifacts
https://pypi.python.org/pypi/appveyor-artifacts

Usage:
    appveyor-artifacts [options] download
    appveyor-artifacts -h | --help
    appveyor-artifacts -V | --version

Options:
    -C DIR --dir=DIR            Download to DIR instead of cwd.
    -c SHA --commit=SHA         Git commit currently building.
    -h --help                   Show this screen.
    -j --always-job-dirs        Always download files within ./<jobID>/ dirs.
    -J MODE --no-job-dirs=MODE  All jobs download to same directory. Modes for
                                file path collisions: rename, overwrite, skip
    -n NAME --repo-name=NAME    Repository name.
    -N JOB --job-name=JOB       Filter by job name (Python versions, etc).
    -o NAME --owner-name=NAME   Repository owner/account name.
    -p NUM --pull-request=NUM   Pull request number of current job.
    -r --raise                  Don't handle exceptions, raise all the way.
    -t NAME --tag-name=NAME     Tag name that triggered current job.
    -T NUM --timeout=NUM        Wait up to NUM seconds of inactivity.
    -v --verbose                Raise exceptions with tracebacks.
    -V --version                Print appveyor-artifacts version.
"""

import functools
import logging
import os
import re
import signal
import sys
import time

import pkg_resources
import requests
import requests.exceptions
from docopt import docopt

API_PREFIX = 'https://ci.appveyor.com/api'
REGEX_COMMIT = re.compile(r'^[0-9a-f]{7,40}$')
REGEX_GENERAL = re.compile(r'^[0-9a-zA-Z\._-]+$')
SLEEP_FOR = 5


class HandledError(Exception):
    """Generic exception used to signal raise HandledError() in scripts."""

    pass


class InfoFilter(logging.Filter):
    """Filter out non-info and non-debug logging statements.

    From: https://stackoverflow.com/questions/16061641/python-logging-split/16066513#16066513
    """

    def filter(self, record):
        """Filter method.

        :param record: Log record object.

        :return: Keep or ignore this record.
        :rtype: bool
        """
        return record.levelno <= logging.INFO


def setup_logging(verbose=False, logger=None):
    """Setup console logging. Info and below go to stdout, others go to stderr.

    :param bool verbose: Print debug statements.
    :param str logger: Which logger to set handlers to. Used for testing.
    """
    format_ = '%(asctime)s %(levelname)-8s %(name)-40s %(message)s' if verbose else '%(message)s'
    level = logging.DEBUG if verbose else logging.INFO

    handler_stdout = logging.StreamHandler(sys.stdout)
    handler_stdout.setFormatter(logging.Formatter(format_))
    handler_stdout.setLevel(logging.DEBUG)
    handler_stdout.addFilter(InfoFilter())

    handler_stderr = logging.StreamHandler(sys.stderr)
    handler_stderr.setFormatter(logging.Formatter(format_))
    handler_stderr.setLevel(logging.WARNING)

    root_logger = logging.getLogger(logger)
    root_logger.setLevel(level)
    root_logger.addHandler(handler_stdout)
    root_logger.addHandler(handler_stderr)


def with_log(func):
    """Automatically adds a named logger to a function upon function call.

    :param func: Function to decorate.

    :return: Decorated function.
    :rtype: function
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        """Inject `log` argument into wrapped function."""
        decorator_logger = logging.getLogger('@with_log')
        decorator_logger.debug('Entering %s() function call.', func.__name__)
        log = kwargs.get('log', logging.getLogger(func.__name__))
        try:
            ret = func(log=log, *args, **kwargs)
        finally:
            decorator_logger.debug('Exiting %s() function call.', func.__name__)
        return ret
    return wrapper


def get_arguments(argv=None, environ=None):
    """Get command line arguments or values from environment variables.

    :param list argv: Command line argument list to process. For testing.
    :param dict environ: Environment variables. For testing.

    :return: Parsed options.
    :rtype: dict
    """
    name = 'appveyor-artifacts'
    environ = environ or os.environ
    require = getattr(pkg_resources, 'require')  # Stupid linting error.
    commit, owner, pull_request, repo, tag = '', '', '', '', ''

    # Run docopt.
    project = [p for p in require(name) if p.project_name == name][0]
    version = project.version
    args = docopt(__doc__, argv=argv or sys.argv[1:], version=version)

    # Handle Travis environment variables.
    if environ.get('TRAVIS') == 'true':
        commit = environ.get('TRAVIS_COMMIT', '')
        owner = environ.get('TRAVIS_REPO_SLUG', '/').split('/')[0]
        pull_request = environ.get('TRAVIS_PULL_REQUEST', '')
        if pull_request == 'false':
            pull_request = ''
        repo = environ.get('TRAVIS_REPO_SLUG', '/').split('/')[1]
        tag = environ.get('TRAVIS_TAG', '')

    # Command line arguments override.
    commit = args['--commit'] or commit
    owner = args['--owner-name'] or owner
    pull_request = args['--pull-request'] or pull_request
    repo = args['--repo-name'] or repo
    tag = args['--tag-name'] or tag

    # Merge env variables and have command line args override.
    config = dict(
        always_job_dirs=args['--always-job-dirs'],
        commit=commit,
        dir=args['--dir'] or '',
        job_name=args['--job-name'] or '',
        no_job_dirs=args['--no-job-dirs'] or '',
        owner=owner,
        pull_request=pull_request,
        repo=repo,
        tag=tag,
        timeout=args['--timeout'] or '',
        verbose=args['--verbose'],
    )

    return config


@with_log
def query_api(endpoint, log):
    """Query the AppVeyor API.

    :raise HandledError: On non HTTP200 responses or invalid JSON response.

    :param str endpoint: API endpoint to query (e.g. '/projects/Robpol86/appveyor-artifacts').

    :return: Parsed JSON response.
    :rtype: dict
    """
    url = API_PREFIX + endpoint
    headers = {'content-type': 'application/json'}
    log.debug('Querying %s with headers %s.', url, headers)
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.Timeout):
        log.error('Timed out waiting for reply from server.')
        raise HandledError
    log.debug('Response status: %d', response.status_code)
    log.debug('Response headers: %s', str(response.headers))
    log.debug('Response text: %s', response.text)

    if not response.ok:
        message = response.json().get('message')
        if message:
            log.error('HTTP %d: %s', response.status_code, message)
        else:
            log.error('HTTP %d: Unknown error: %s', response.status_code, response.text)
        raise HandledError

    try:
        return response.json()
    except ValueError:
        log.error('Failed to parse JSON: %s', response.text)
        raise HandledError


@with_log
def validate(config, log):
    """Validate config values.

    :raise HandledError: On invalid config values.

    :param dict config: Dictionary from get_arguments().
    """
    if config['always_job_dirs'] and config['no_job_dirs']:
        log.error('Contradiction: --always-job-dirs and --no-job-dirs used.')
        raise HandledError
    if config['commit'] and not REGEX_COMMIT.match(config['commit']):
        log.error('No or invalid git commit obtained.')
        raise HandledError
    if config['dir'] and not os.path.isdir(config['dir']):
        log.error("Not a directory or doesn't exist: %s", config['dir'])
        raise HandledError
    if config['no_job_dirs'] not in ('', 'rename', 'overwrite', 'skip'):
        log.error('--no-job-dirs has invalid value. Check --help for valid values.')
        raise HandledError
    if not config['owner'] or not REGEX_GENERAL.match(config['owner']):
        log.error('No or invalid repo owner name obtained.')
        raise HandledError
    if config['pull_request'] and not config['pull_request'].isdigit():
        log.error('--pull-request is not a digit.')
        raise HandledError
    if not config['repo'] or not REGEX_GENERAL.match(config['repo']):
        log.error('No or invalid repo name obtained.')
        raise HandledError
    if config['tag'] and not REGEX_GENERAL.match(config['tag']):
        log.error('Invalid git tag obtained.')
        raise HandledError
    if config['timeout'] and not config['timeout'].isdigit():
        log.error('--timeout is not a digit.')
        raise HandledError


@with_log
def get_build_version(config, log):
    """Find the build version we're looking for.

    AppVeyor calls build IDs "versions" which is confusing but whatever. Job IDs aren't available in the history query,
    only on latest, specific version, and deployment queries. Hence we need two queries to get a one-time status update.

    Returns None if the job isn't queued yet.

    :raise HandledError: On invalid JSON data.

    :param dict config: Dictionary from get_arguments().

    :return: Build version.
    :rtype: str
    """
    url = '/projects/{0}/{1}/history?recordsNumber=10'.format(config['owner'], config['repo'])

    # Query history.
    log.debug('Querying AppVeyor history API for %s/%s...', config['owner'], config['repo'])
    json_data = query_api(url)
    if 'builds' not in json_data:
        log.error('Bad JSON reply: "builds" key missing.')
        raise HandledError

    # Find AppVeyor build "version".
    for build in json_data['builds']:
        if config['tag'] and config['tag'] == build.get('tag'):
            log.debug('This is a tag build.')
        elif config['pull_request'] and config['pull_request'] == build.get('pullRequestId'):
            log.debug('This is a pull request build.')
        elif config['commit'] == build['commitId']:
            log.debug('This is a branch build.')
        else:
            continue
        log.debug('Build JSON dict: %s', str(build))
        return build['version']
    return None


@with_log
def get_job_ids(build_version, config, log):
    """Get one or more job IDs and their status associated with a build version.

    Filters jobs by name if --job-name is specified.

    :raise HandledError: On invalid JSON data or bad job name.

    :param str build_version: AppVeyor build version from get_build_version().
    :param dict config: Dictionary from get_arguments().

    :return: List of two-item tuples. Job ID (first) and its status (second).
    :rtype: list
    """
    url = '/projects/{0}/{1}/build/{2}'.format(config['owner'], config['repo'], build_version)

    # Query version.
    log.debug('Querying AppVeyor version API for %s/%s at %s...', config['owner'], config['repo'], build_version)
    json_data = query_api(url)
    if 'build' not in json_data:
        log.error('Bad JSON reply: "build" key missing.')
        raise HandledError
    if 'jobs' not in json_data['build']:
        log.error('Bad JSON reply: "jobs" key missing.')
        raise HandledError

    # Find AppVeyor job.
    all_jobs = list()
    for job in json_data['build']['jobs']:
        if config['job_name'] and config['job_name'] == job['name']:
            log.debug('Filtering by job name: found match!')
            return [(job['jobId'], job['status'])]
        all_jobs.append((job['jobId'], job['status']))
    if config['job_name']:
        log.error('Job name "%s" not found.', config['job_name'])
        raise HandledError
    return all_jobs


@with_log
def get_artifacts_urls(job_ids, log):
    """Query API again for artifacts' urls.

    :param iter job_ids: List of AppVeyor jobIDs.

    :return: All artifacts' URLs, list of 2-item tuples (job id, url suffix).
    :rtype: list
    """
    artifacts = list()
    for job in job_ids:
        url = '/buildjobs/{0}/artifacts'.format(job)
        log.debug('Querying AppVeyor artifact API for %s/%s at %s...', job)
        json_data = query_api(url)
        for artifact in json_data:
            file_name = artifact['fileName']
            artifacts.append((job, file_name))
    return artifacts


@with_log
def main(config, log):
    """Main function. Runs the program.

    :param dict config: Dictionary from get_arguments().
    """
    validate(config)

    # Wait for job to be queued. Once it is we'll have the "version".
    build_version = None
    for _ in range(3):
        build_version = get_build_version(config)
        if build_version:
            break
        log.info('Waiting for job to be queued...')
        time.sleep(SLEEP_FOR)
    if not build_version:
        log.error('Timed out waiting for job to be queued or build not found.')
        raise HandledError

    # Get job IDs. Wait for AppVeyor job to finish.
    job_ids = list()
    start_time = time.time()
    valid_statuses = ['success', 'failed', 'running', 'queued']
    while True:
        job_ids = get_job_ids(build_version, config)
        statuses = set([i[1] for i in job_ids])
        if 'failed' in statuses:
            job = [i[0] for i in job_ids if i[1] == 'failed'][0]
            url = 'https://ci.appveyor.com/project/{0}/{1}/build/job/{2}'.format(config['owner'], config['repo'], job)
            log.error('AppVeyor job failed: %s', url)
            raise HandledError
        if statuses == set(valid_statuses[:1]):
            log.info('Build successful. Found %d job%s.', len(job_ids), '' if len(job_ids) == 1 else 's')
            break
        if 'running' in statuses:
            log.info('Waiting for job%s to finish...', '' if len(job_ids) == 1 else 's')
        elif 'queued' in statuses:
            log.info('Waiting for all jobs to start...')
        else:
            log.error('Got unknown status from AppVeyor API: %s', statuses - valid_statuses)
            raise HandledError
        if config['timeout'] and time.time() - start_time >= config['timeout']:
            log.error('Timed out waiting for job%s to finish.', '' if len(job_ids) == 1 else 's')
            raise HandledError
        time.sleep(SLEEP_FOR)

    # Get artifacts' URLs.
    artifacts = get_artifacts_urls([i[0] for i in job_ids])
    log.info('Found %d artifact%s.', len(artifacts), '' if len(artifacts) == 1 else 's')
    if not artifacts:
        log.warning('No artifacts; nothing to download.')
        return


def entry_point():
    """Entry-point from setuptools."""
    signal.signal(signal.SIGINT, lambda *_: getattr(os, '_exit')(0))  # Properly handle Control+C
    config = get_arguments()
    setup_logging(config['verbose'])
    try:
        main(config)
    except HandledError:
        if config['--raise']:
            raise
        logging.critical('Failure.')
        sys.exit(1)


if __name__ == '__main__':
    entry_point()
