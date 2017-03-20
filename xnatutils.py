import os.path
import re
import stat
import getpass
import xnat
from xnat.exceptions import XNATResponseError
import warnings

MBI_XNAT_SERVER = 'https://mbi-xnat.erc.monash.edu.au'

data_format_exts = {
    'NIFTI': '.nii',
    'NIFTI_GZ': '.nii.gz',
    'MRTRIX': '.mif',
    'DICOM': '',
    'secondary': '',
    'TEXT_MATRIX': '.mat',
    'MRTRIX_GRAD': '.b',
    'FSL_BVECS': '.bvec',
    'FSL_BVALS': '.bval',
    'MATLAB': '.mat',
    'ANALYZE': '.img',
    'ZIP': '.zip',
    'RDATA': '.rdata'}


sanitize_re = re.compile(r'[^a-zA-Z_0-9]')
# TODO: Need to add other illegal chars here
illegal_scan_chars_re = re.compile(r'\.')


class XnatUtilsUsageError(Exception):
    pass


class XnatUtilsLookupError(XnatUtilsUsageError):

    def __init__(self, path):
        self.path = path

    def __str__(self):
        return ("Could not find asset corresponding to '{}' (please make sure"
                " you have access to it if it exists)".format(self.path))


def connect(user=None, loglevel='ERROR'):
    netrc_path = os.path.join(os.environ['HOME'], '.netrc')
    if user is not None or not os.path.exists(netrc_path):
        if user is None:
            user = raw_input('username: ')
        password = getpass.getpass()
        save_netrc = raw_input(
            "Would you like to save this username/password in your ~/.netrc "
            "(with 600 permissions) [y/N]: ")
        if save_netrc.lower() in ('y', 'yes'):
            with open(netrc_path, 'w') as f:
                f.write(
                    "machine {}\n".format(MBI_XNAT_SERVER.split('/')[-1]) +
                    "user {}\n".format(user) +
                    "password {}\n".format(password))
            os.chmod(netrc_path, stat.S_IRUSR | stat.S_IWUSR)
            print ("MBI-XNAT username and password for user '{}' saved in {}"
                   .format(user, os.path.join(os.environ['HOME'], '.netrc')))
    kwargs = ({'user': user, 'password': password}
              if not os.path.exists(netrc_path) else {})
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return xnat.connect(MBI_XNAT_SERVER, loglevel=loglevel, **kwargs)


def extract_extension(filename):
    name_parts = os.path.basename(filename).split('.')
    if len(name_parts) == 1:
        ext = ''
    else:
        if name_parts[-1] == 'gz':
            num_parts = 2
        else:
            num_parts = 1
        ext = '.' + '.'.join(name_parts[-num_parts:])
    return ext


def get_data_format(filename):
    try:
        return next(k for k, v in data_format_exts.iteritems()
                    if v == extract_extension(filename))
    except StopIteration:
        raise XnatUtilsUsageError(
            "No format matching extension '{}' (of '{}')"
            .format(extract_extension(filename), filename))


def get_extension(data_format):
    return data_format_exts[data_format]


def is_regex(ids):
    "Checks to see if string contains special characters"
    if isinstance(ids, basestring):
        ids = [ids]
    return not all(re.match(r'^\w+$', i) for i in ids)


def list_results(mbi_xnat, path, attr):
    try:
        response = mbi_xnat.get_json('/data/archive/' + path)
    except XNATResponseError as e:
        match = re.search(r'\(status (\d+)\)', str(e))
        if match:
            status_code = int(match.group(1))
        else:
            status_code = None
        if status_code == 404:
            raise XnatUtilsLookupError(path)
        else:
            raise XnatUtilsUsageError(str(e))
    if 'ResultSet' in response:
        results = [r[attr] for r in response['ResultSet']['Result']]
    else:
        results = [r['data_fields'][attr]
                   for r in response['items'][0]['children'][0]['items']]
    return results


def matching_subjects(mbi_xnat, subject_ids):
    if is_regex(subject_ids):
        all_subjects = list_results(mbi_xnat, 'subjects', attr='label')
        subjects = [s for s in all_subjects
                    if any(re.match(i, s) for i in subject_ids)]
    else:
        subjects = set()
        for id_ in subject_ids:
            try:
                subjects.update(
                    list_results(
                        mbi_xnat, 'projects/{}/subjects'.format(id_), 'label'))
            except XnatUtilsLookupError:
                raise XnatUtilsUsageError(
                    "No project named '{}' (that you have access to)"
                    .format(id_))
        subjects = list(subjects)
    return subjects


def matching_sessions(mbi_xnat, session_ids):
    if isinstance(session_ids, basestring):
        session_ids = [session_ids]
    if is_regex(session_ids):
        all_sessions = list_results(mbi_xnat, 'experiments', attr='label')
        sessions = [s for s in all_sessions
                    if any(re.match(i, s) for i in session_ids)]
    else:
        sessions = set()
        for id_ in session_ids:
            if '_' not in id_:
                try:
                    project = mbi_xnat.projects[id_]
                except KeyError:
                    raise XnatUtilsUsageError(
                        "No project named '{}'".format(id_))
                sessions.update(list_results(
                    mbi_xnat, 'projects/{}/experiments'.format(project.id),
                    'label'))
            elif id_ .count('_') == 1:
                try:
                    subject = mbi_xnat.subjects[id_]
                except KeyError:
                    raise XnatUtilsUsageError(
                        "No subject named '{}'".format(id_))
                sessions.update(list_results(
                    mbi_xnat, 'subjects/{}/experiments'.format(subject.id),
                    'label'))
            elif id_ .count('_') == 2:
                sessions.add(id_)
            else:
                raise XnatUtilsUsageError(
                    "Invalid ID '{}' for listing sessions "
                    .format(id_))
        sessions = list(sessions)
    return sessions


def matching_scans(session, scan_types):
    return [s for s in session.scans.itervalues() if (
        scan_types is None or
        any(re.match(i + '$', s.type) for i in scan_types))]


def find_executable(name):
    """
    Finds the location of an executable on the system path

    Parameters
    ----------
    name : str
        Name of the executable to search for on the system path
    """
    path = None
    for path_prefix in os.environ['PATH'].split(os.path.pathsep):
        prov_path = os.path.join(path_prefix, name)
        if os.path.exists(prov_path):
            path = prov_path
    return path


if __name__ == '__main__':
    with connect() as mbi_xnat:
        print '\n'.join(matching_sessions(mbi_xnat, 'MRH06.*_MR01'))
