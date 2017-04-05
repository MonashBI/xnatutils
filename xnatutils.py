import os.path
import re
import subprocess as sp
import errno
import shutil
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
    'RDATA': '.rdata',
    'DAT': '.dat'}


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


def get(session, download_dir, scan=None, data_format=None,
             convert_to=None, converter=None, subject_dirs=False, user=None,
             strip_name=False):
    """
    Downloads datasets (e.g. scans) from MBI-XNAT.

    By default all scans in the provided session(s) are downloaded to the
    current working directory unless they are filtered by the provided 'scan'
    kwarg. Both the session name and scan filters can be regular
    expressions, e.g.

        >>> xnat_get('MRH017_001_MR.*', scan='ep2d_diff.*')

    The destination directory can be specified by the 'directory' kwarg.
    Each session will be downloaded to its own folder under the destination
    directory unless the 'subject-dir' kwarg is provided in which case the
    sessions will be grouped under separate subject directories.

    If there are multiple resources for a dataset on MBI-XNAT (unlikely) the
    one to download can be specified using the 'data_format' kwarg, otherwise
    the only recognised neuroimaging format (e.g. DICOM, NIfTI, MRtrix format).

    DICOM files (ONLY DICOM file) name can be stripped using the kwarg
    'strip_name'. If specified, the final name will be in the format
    000*.dcm.

    The downloaded images can be automatically converted to NIfTI or MRtrix
    formats using dcm2niix or mrconvert (if the tools are installed and on the
    system path) by providing the 'convert_to' kwarg and specifying the
    desired format.

        >>> xnat_get('TEST001_001_MR01', scan='ep2d_diff.*',
                     convert_to='nifti_gz')

    User credentials can be stored in a ~/.netrc file so that they don't need
    to be entered each time a command is run. If a new user provided or netrc
    doesn't exist the tool will ask whether to create a ~/.netrc file with the
    given credentials.
    """
    with connect(user) as mbi_xnat:
        num_sessions = 0
        num_scans = 0
        matched_sessions = matching_sessions(mbi_xnat, session)
        if not matched_sessions:
            raise XnatUtilsUsageError(
                "No accessible sessions matched pattern(s) '{}'"
                .format("', '".join(session)))
        for session_label in matched_sessions:
            exp = mbi_xnat.experiments[session_label]
            for scan in matching_scans(exp, scan):
                scan_name = sanitize_re.sub('_', scan.type)
                scan_label = scan.id + '-' + scan_name
                if data_format is not None:
                    data_format = data_format.upper()
                else:
                    data_formats = [
                        r.label for r in scan.resources.itervalues()
                        if r.label in data_format_exts]
                    if not data_formats:
                        raise XnatUtilsUsageError(
                            "No valid scan formats for '{}-{}' in '{}'"
                            .format(scan.id, scan.type, session))
                    elif len(data_formats) > 1:
                        raise XnatUtilsUsageError(
                            "Multiple valid scan formats for '{}' in '{}' "
                            "('{}') please specify one using the --scan option"
                            .format(scan_name, session,
                                    "', '".join(data_formats)))
                    data_format = data_formats[0]
                # Get the target location for the downloaded scan
                if subject_dirs:
                    parts = session_label.split('_')
                    target_dir = os.path.join(download_dir,
                                               '_'.join(parts[:2]), parts[-1])
                else:
                    target_dir = os.path.join(download_dir, session_label)
                try:
                    os.makedirs(target_dir)
                except OSError as e:
                    if e.errno != errno.EEXIST:
                        raise
                if convert_to:
                    target_ext = data_format_exts[convert_to.upper()]
                else:
                    target_ext = get_extension(data_format)
                target_path = os.path.join(target_dir,
                                           scan_label + target_ext)
                tmp_dir = target_path + '.download'
                # Download the scan from XNAT
                print 'Downloading {}: {}'.format(exp.label, scan_label)
                scan.resources[data_format].download_dir(tmp_dir)
                # Extract the relevant data from the download dir and move to
                # target location

                src_path = os.path.join(tmp_dir, session_label, 'scans',
                                        scan_label, 'resources',
                                        data_format, 'files')
                if data_format not in ('DICOM', 'secondary'):
                    src_path = (os.path.join(src_path, scan_name) +
                                data_format_exts[data_format])
                # Convert or move downloaded dir/files to target path
                dcm2niix = find_executable('dcm2niix')
                mrconvert = find_executable('mrconvert')
                if converter == 'dcm2niix':
                    if dcm2niix is None:
                        raise XnatUtilsUsageError(
                            "Selected converter 'dcm2niix' is not available, "
                            "please make sure it is installed and on your "
                            "path")
                    mrconvert = None
                elif converter == 'mrconvert':
                    if mrconvert is None:
                        raise XnatUtilsUsageError(
                            "Selected converter 'mrconvert' is not available, "
                            "please make sure it is installed and on your "
                            "path")
                    dcm2niix = None
                else:
                    assert converter is None
                try:
                    if (convert_to is None or
                            convert_to.upper() == data_format):
                        # No conversion required
                        if strip_name and data_format in ('DICOM',
                                                               'secondary'):
                            dcmfiles = sorted(os.listdir(src_path))
                            tmp_path = os.path.join(
                                target_dir, scan_label + target_ext)
                            os.mkdir(tmp_path)
                            for i, f in enumerate(dcmfiles):
                                tmp_src_path = os.path.join(
                                    src_path, f)
                                tmp_target_path = os.path.join(
                                    tmp_path, str(i + 1).zfill(4) + '.dcm')
                                shutil.move(tmp_src_path, tmp_target_path)
                        else:
                            shutil.move(src_path, target_path)
                    elif (convert_to in ('nifti', 'nifti_gz') and
                          data_format == 'DICOM' and dcm2niix is not None):
                        # convert between dicom and nifti using dcm2niix.
                        # mrconvert can do this as well but there have been
                        # some problems losing TR from the dicom header.
                        zip_opt = 'y' if convert_to == 'nifti_gz' else 'n'
                        sp.check_call('{} -z {} -o {} -f {} {}'.format(
                            dcm2niix, zip_opt, target_dir, scan_label,
                            src_path), shell=True)
                    elif mrconvert is not None:
                        # If dcm2niix format is not installed or another is
                        # required use mrconvert instead.
                        sp.check_call('{} {} {}'.format(
                            mrconvert, src_path, target_path), shell=True)
                    else:
                        if (data_format == 'DICOM' and
                                convert_to in ('nifti', 'nifti_gz')):
                            msg = 'either dcm2niix or '
                        raise XnatUtilsUsageError(
                            "Please install {} mrconvert to convert between {}"
                            "and {} formats".format(msg, data_format.lower(),
                                                    convert_to))
                except sp.CalledProcessError as e:
                    shutil.move(src_path, os.path.join(
                        target_dir,
                        scan_label + data_format_exts[data_format]))
                    print ("WARNING! Could not convert {}:{} to {} format ({})"
                           .format(exp.label, scan.type, convert_to,
                                   (e.output.strip() if e.output is not None
                                    else '')))
                # Clean up download dir
                shutil.rmtree(tmp_dir)
                num_scans += 1
            num_sessions += 1
        if not num_scans:
            print ("No scans matched pattern(s) '{}' in specified sessions ({}"
                   ")".format(("', '".join(scan) if scan is not None
                               else ''), "', '".join(matched_sessions)))
        else:
            print "Successfully downloaded {} scans from {} sessions".format(
                num_scans, num_sessions)


def ls(xnat_id, datatype=None, user=None):
    """
    Displays available projects, subjects, sessions and scans from MBI-XNAT.

    The datatype listed (i.e. 'project', 'subject', 'session' or 'scan') is
    assumed to be the next level down the data tree if not explicitly provided
    (i.e. subjects if a project ID is provided, sessions if a subject ID is
    provided, etc...) but can be explicitly provided via the '--datatype'
    option. For example, to list all sessions within the MRH001 project

        >>> xnat_ls('MRH001', datatype='session')

    Scans listed over multiple sessions will be added to a set, so the list
    returned is the list of unique scan types within the specified sessions. If
    no arguments are provided the projects the user has access to will be
    listed.

    Multiple subject or session IDs can be provided as a sequence or using
    regular expression syntax (e.g. MRH000_.*_MR01 will match the first session
    for each subject in project MRH000). Note that if regular expressions are
    used then an explicit datatype must also be provided.

    User credentials can be stored in a ~/.netrc file so that they don't need
    to be entered each time a command is run. If a new user provided or netrc
    doesn't exist the tool will ask whether to create a ~/.netrc file with the
    given credentials.

    Parameters
    ----------
    xnat_id : str
        The ID of the project/subject/session to list from
    datatype : str
        The data type to list, can be one of 'project', 'subject', 'session'
        or 'scan'
    user : str
        The user to connect to MBI-XNAT with
    """
    if datatype is None:
        if not xnat_id:
            datatype = 'project'
        else:
            if is_regex(xnat_id):
                raise XnatUtilsUsageError(
                    "'--datatype' option must be provided if using regular "
                    "expression id, '{}' (i.e. one with non alphanumeric + '_'"
                    " characters in it)".format("', '".join(xnat_id)))
            num_underscores = max(i.count('_') for i in xnat_id)
            if num_underscores == 0:
                datatype = 'subject'
            elif num_underscores == 1:
                datatype = 'session'
            elif num_underscores == 2:
                datatype = 'scan'
            else:
                raise XnatUtilsUsageError(
                    "Invalid ID(s) provided '{}'".format(
                        "', '".join(i for i in xnat_id if i.count('_') > 2)))
    else:
        datatype = datatype
        if datatype == 'project':
            if xnat_id:
                raise XnatUtilsUsageError(
                    "IDs should not be provided for 'project' datatypes ('{}')"
                    .format("', '".join(xnat_id)))
        else:
            if not xnat_id:
                raise XnatUtilsUsageError(
                    "IDs must be provided for '{}' datatype listings"
                    .format(datatype))

    with connect(user) as mbi_xnat:
        if datatype == 'project':
            return sorted(list_results(mbi_xnat, 'projects', 'ID'))
        elif datatype == 'subject':
            return sorted(matching_subjects(mbi_xnat, xnat_id))
        elif datatype == 'session':
            return sorted(matching_sessions(mbi_xnat, xnat_id))
        elif datatype == 'scan':
            if not is_regex(xnat_id) and len(xnat_id) == 1:
                exp = mbi_xnat.experiments[xnat_id[0]]
                return sorted(list_results(
                    mbi_xnat, 'experiments/{}/scans'.format(exp.id), 'type'))
            else:
                scans = set()
                for session in matching_sessions(mbi_xnat, xnat_id):
                    exp = mbi_xnat.experiments[session]
                    session_scans = set(list_results(
                        mbi_xnat, 'experiments/{}/scans'.format(exp.id),
                        'type'))
                    scans |= session_scans
                return sorted(scans)
        else:
            assert False


def put(filename, session, scan, overwrite=False, create_session=False,
        data_format=None, user=None):
    """
    Uploads datasets to a MBI-XNAT project (requires manager privileges for the
    project).

    The format of the uploaded file is guessed from the file extension
    (recognised extensions are '.nii', '.nii.gz', '.mif'), the scan entry is
    created in the session and if 'create_session' kwarg is True the
    subject and session are created if they are not already present, e.g.

        >>> xnat_put('test.nii.gz', 'TEST001_001_MR01', 'a_dataset',
                     create_session=True)

    NB: If the scan already exists the 'overwrite' kwarg must be provided to
    overwrite it.

    User credentials can be stored in a ~/.netrc file so that they don't need
    to be entered each time a command is run. If a new user provided or netrc
    doesn't exist the tool will ask whether to create a ~/.netrc file with the
    given credentials.

    Parameters
    ----------
    filename : str
        help="Filename of the dataset to upload to XNAT
    session : str
        Name of the session to upload the dataset to
    scan : str
        Name for the dataset on XNAT
    overwrite : bool
        Allow overwrite of existing dataset
    create_session : bool
        Create the required session on XNAT to upload the the dataset to
    format : str
        The name of the resource (the data format) to
        upload the dataset to. If not provided the format
        will be determined from the file extension (i.e.
        in most cases it won't be necessary to specify
    user : str
        The user to connect to MBI-XNAT with
    """
    if not os.path.exists(filename):
        raise XnatUtilsUsageError(
            "The file to upload, '{}', does not exist".format(filename))
    if sanitize_re.match(session) or session.count('_') != 2:
        raise XnatUtilsUsageError(
            "Session '{}' is not a valid session name (must only contain "
            "alpha-numeric characters and exactly two underscores")
    if illegal_scan_chars_re.search(scan) is not None:
        raise XnatUtilsUsageError(
            "Scan name '{}' contains illegal characters".format(scan))

    if data_format is None:
        data_format = get_data_format(filename)
        ext = extract_extension(filename)
    else:
        data_format = data_format.upper()
        try:
            ext = data_format_exts[data_format]
        except KeyError:
            ext = extract_extension(filename)
    with connect(user) as mbi_xnat:
        try:
            session = mbi_xnat.experiments[session]
        except KeyError:
            if create_session:
                project_id = session.split('_')[0]
                subject_id = '_'.join(session.split('_')[:2])
                try:
                    project = mbi_xnat.projects[project_id]
                except KeyError:
                    raise XnatUtilsUsageError(
                        "Cannot create session '{}' as '{}' does not exist "
                        "(or you don't have access to it)".format(session,
                                                                  project_id))
                # Creates a corresponding subject and session if they don't
                # exist
                subject = mbi_xnat.classes.SubjectData(label=subject_id,
                                                       parent=project)
                session = mbi_xnat.classes.MrSessionData(
                    label=session, parent=subject)
                print "{} session successfully created.".format(session.label)
            else:
                raise XnatUtilsUsageError(
                    "'{}' session does not exist, to automatically create it "
                    "please use '--create_session' option."
                    .format(session))
        dataset = mbi_xnat.classes.MrScanData(type=scan, parent=session)
        if overwrite:
            try:
                dataset.resources[data_format].delete()
                print "Deleted existing dataset at {}:{}".format(
                    session, scan)
            except KeyError:
                pass
        resource = dataset.create_resource(data_format)
        resource.upload(filename, scan + ext)
        print "{} successfully uploaded to {}:{}".format(
            filename, session, scan)


def varget(subject_or_session_id, variable, default='', user=None):
    """
    Gets the value of a variable (custom or otherwise) of a session or subject
    in a MBI-XNAT project

    User credentials can be stored in a ~/.netrc file so that they don't need
    to be entered each time a command is run. If a new user provided or netrc
    doesn't exist the tool will ask whether to create a ~/.netrc file with the
    given credentials.

    Parameters
    ----------
    subject_or_session_id : str
        Name of subject or session to set the variable of
    variable : str
        Name of the variable to set
    default : str
        Default value if object does not have a value
    user : str
        The user to connect to MBI-XNAT with
    """
    with connect(user) as mbi_xnat:
        # Get XNAT object to set the field of
        if subject_or_session_id.count('_') == 1:
            xnat_obj = mbi_xnat.subjects[subject_or_session_id]
        elif subject_or_session_id.count('_') == 2:
            xnat_obj = mbi_xnat.experiments[subject_or_session_id]
        else:
            raise XnatUtilsUsageError(
                "Invalid ID '{}' for subject or sessions (must contain one "
                "underscore for subjects and two underscores for sessions)"
                .format(subject_or_session_id))
        # Get value
        try:
            return xnat_obj.fields[variable]
        except KeyError:
            return default


def varput(subject_or_session_id, variable, value, user=None):
    """
    Sets variables (custom or otherwise) of a session or subject in a MBI-XNAT
    project

    User credentials can be stored in a ~/.netrc file so that they don't need
    to be entered each time a command is run. If a new user provided or netrc
    doesn't exist the tool will ask whether to create a ~/.netrc file with the
    given credentials.

    Parameters
    ----------
    subject_or_session_id : str
        Name of subject or session to set the variable of
    variable : str
        Name of the variable to set
    value : str
        Value to set the variable to
    user : str
        The user to connect to MBI-XNAT with
    """
    with connect(user) as mbi_xnat:
        # Get XNAT object to set the field of
        if subject_or_session_id.count('_') == 1:
            xnat_obj = mbi_xnat.subjects[subject_or_session_id]
        elif subject_or_session_id.count('_') == 2:
            xnat_obj = mbi_xnat.experiments[subject_or_session_id]
        else:
            raise XnatUtilsUsageError(
                "Invalid ID '{}' for subject or sessions (must contain one "
                "underscore  for subjects and two underscores for sessions)"
                .format(subject_or_session_id))
        # Set value
        xnat_obj.fields[variable] = value


def connect(user=None, loglevel='ERROR'):
    """
    Opens a connection to MBI-XNAT

    Parameters
    ----------
    user : str
        The username to connect with. If None then tries to load the username
        from the $HOME/.netrc file
    loglevel : str
        The logging level to display. In order of increasing verbosity ERROR,
        WARNING, INFO, DEBUG.
    """
    netrc_path = os.path.join(os.environ['HOME'], '.netrc')
    if user is not None or not os.path.exists(netrc_path):
        if user is None:
            user = raw_input('authcate/username: ')
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
        try:
            return xnat.connect(MBI_XNAT_SERVER, loglevel=loglevel, **kwargs)
        except TypeError:
            # If using XnatPy < 0.2.3
            return xnat.connect(MBI_XNAT_SERVER, debug=(loglevel == 'DEBUG'),
                                **kwargs)


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
