"""Exact repository-bound workflow ownership for pending Check Runs."""
import re
from urllib.parse import urlsplit


def workflow_owner_id(details_url, repository, server_url='https://github.com'):
    if not isinstance(details_url, str) or not isinstance(repository, str):
        return None
    try:
        url, server = urlsplit(details_url), urlsplit(server_url)
        if (url.scheme != 'https' or url.netloc != server.netloc or
                url.username or url.password or url.query or url.fragment):
            return None
        match = re.fullmatch(re.escape('/' + repository) +
                             r'/actions/runs/([1-9][0-9]*)(?:/(?:attempts|job)/[1-9][0-9]*)?', url.path)
        return int(match[1]) if match else None
    except ValueError:
        return None


def orphaned_check(run, repository, read_owner, read_check=None, server_url='https://github.com'):
    """Only a completed owner and a freshly re-read unfinished check establish an orphan."""
    if run.get('status') != 'in_progress':
        return False
    owner_id = workflow_owner_id(run.get('details_url'), repository, server_url)
    if owner_id is None:
        return False
    owner = read_owner(owner_id)
    if not (isinstance(owner, dict) and owner.get('id') == owner_id and owner.get('status') == 'completed'):
        return False
    check_id = run.get('id')
    if read_check is None or not isinstance(check_id, int) or isinstance(check_id, bool):
        return False
    fresh = read_check(check_id)
    if not (isinstance(fresh, dict) and fresh.get('id') == check_id):
        return False
    return fresh.get('status') != 'completed'
