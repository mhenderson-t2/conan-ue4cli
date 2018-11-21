import argparse, conans, copy, glob, os, platform, subprocess, tempfile
from os import path

class DelegateManager(object):
	def __init__(self, delegatesDir):
		
		# Read the contents of the default (no-op) delegate class for generated packages
		self.delegatesDir = delegatesDir
		self.defaultDelegate = conans.tools.load(path.join(self.delegatesDir, '__default.py'))
	
	def getDelegateClass(self, libName):
		'''
		Retrieves the delegate class code for the specified package (if one exists),
		or else returns the default (no-op) delegate class
		'''
		delegateFile = path.join(self.delegatesDir, '{}.py'.format(libName))
		if path.exists(delegateFile):
			return conans.tools.load(delegateFile)
		
		return self.defaultDelegate

def _run(command, cwd=None, env=None):
	'''
	Executes a command and raises an exception if it fails
	'''
	proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, universal_newlines=True)
	(stdout, stderr) = proc.communicate(input)
	if proc.returncode != 0:
		raise Exception(
			'child process {} failed with exit code {}'.format(command, proc.returncode) +
			'\nstdout: "{}"\nstderr: "{}"'.format(stdout, stderr)
		)

def _detectClang(manager):
	'''
	Detects the presence of clang and returns a tuple containing the path to clang and the path to clang++
	'''
	
	# Check if clang is installed without any suffix
	if conans.tools.which('clang++') != None:
		return ('clang', 'clang++')
	
	# Check if clang 3.8 or newer is installed with a version suffix
	for ver in reversed(range(38, 60)):
		suffix = '-{:.1f}'.format(ver / 10.0)
		if conans.tools.which('clang++' + suffix) != None:
			return ('clang' + suffix, 'clang++' + suffix)
	
	# Check if UE4 has a bundled version of clang (introduced in UE4.20.0)
	# (Note that UBT only uses the bundled clang if a system clang is unavailable, so we also need to follow this behaviour)
	engineRoot = manager.getEngineRoot()
	bundledClang = glob.glob(os.path.join(engineRoot, 'Engine/Extras/ThirdPartyNotUE/SDKs/HostLinux/**/bin/clang'), recursive=True)
	if len(bundledClang) != 0:
		return (bundledClang[0], bundledClang[0] + '++')
	
	raise Exception('could not detect clang. Please ensure clang 3.8 or newer is installed.')

def _install(packageDir, channel, profile):
	'''
	Installs a package
	'''
	_run(['conan', 'create', '.', 'adamrehn/' + channel, '--profile', profile], cwd=packageDir)

def _generateWrapper(libName, template, delegates, packageDir, channel, profile):
	'''
	Generates and installs a wrapper package
	'''
	conanfile = template.replace('${LIBNAME}', libName)
	conanfile = conanfile.replace('${DELEGATE_CLASS}', delegates.getDelegateClass(libName))
	conans.tools.save(path.join(packageDir, 'conanfile.py'), conanfile)
	_install(packageDir, channel, profile)

def generate(manager, argv):
	
	# Our supported command-line arguments
	parser = argparse.ArgumentParser(
		prog='ue4 conan generate',
		description = 'Generates the UE4 Conan profile and associated packages'
	)
	parser.add_argument('--profile-only', action='store_true', help='Create the profile and base packages only, skipping wrapper package generation')
	
	# Parse the supplied command-line arguments
	args = parser.parse_args(argv)
	
	# Verify that the detected version of UE4 is new enough
	versionFull = manager.getEngineVersion()
	versionMinor = int(manager.getEngineVersion('minor'))
	if versionMinor < 19:
		print('Warning: the detected UE4 version ({}) is too old (4.19.0 or newer required), skipping installation.'.format(versionFull), file=sys.stderr)
		return
	
	# Determine the full path to the directories containing our files
	scriptDir = path.dirname(path.abspath(__file__))
	packagesDir = path.join(scriptDir, 'packages')
	templateDir = path.join(scriptDir, 'template')
	delegatesDir = path.join(scriptDir, 'delegates')
	
	# Read the contents of the template conanfile for generated packages
	template = conans.tools.load(path.join(templateDir, 'conanfile.py'))
	
	# Create the delegate class manager
	delegates = DelegateManager(delegatesDir)
	
	# Create an auto-deleting temporary directory to hold the generated conanfiles
	with tempfile.TemporaryDirectory() as tempDir:
		
		# Use the Conan profile name "ue4" to maintain clean separation from the default Conan profile
		profile = 'ue4'
		
		# Under Linux, make sure the ue4 Conan profile detects clang instead of GCC
		profileEnv = copy.deepcopy(os.environ)
		if platform.system() == 'Linux':
			clang = _detectClang(manager)
			profileEnv['CC'] = clang[0]
			profileEnv['CXX'] = clang[1]
			print('Detected clang:   {}'.format(clang[0]))
			print('Detected clang++: {}'.format(clang[1]))
		
		print('Removing the "{}" Conan profile if it already exists...'.format(profile))
		profileFile = path.join(conans.paths.get_conan_user_home(), '.conan', 'profiles', profile)
		if path.exists(profileFile):
			os.unlink(profileFile)
		
		print('Creating "{}" Conan profile using autodetected settings...'.format(profile))
		_run(['conan', 'profile', 'new', profile, '--detect'], env=profileEnv)
		
		# Under Linux, update the ue4 Conan profile to force the use of clang and libc++
		if platform.system() == 'Linux':
			_run(['conan', 'profile', 'update', 'env.CC={}'.format(profileEnv['CC']), profile])
			_run(['conan', 'profile', 'update', 'env.CXX={}'.format(profileEnv['CXX']), profile])
			_run(['conan', 'profile', 'update', 'settings.compiler.libcxx=libc++', profile])
		
		print('Removing any previous versions of profile base packages...')
		_run(['conan', 'remove', '--force', '*@adamrehn/profile'])
		
		print('Installing profile base packages...')
		_install(path.join(packagesDir, 'ue4lib'), 'profile', profile)
		_install(path.join(packagesDir, 'libcxx'), 'profile', profile)
		_install(path.join(packagesDir, 'ue4util'), 'profile', profile)
		
		# If we are only creating the Conan profile, stop processing here
		if args.profile_only == True:
			print('Skipping wrapper package generation.')
			return
		
		# Use the short form of the UE4 version string (e.g 4.19) as the channel for our installed packages
		channel = manager.getEngineVersion('short')
		
		print('Retrieving thirdparty library list from UBT...')
		libs = [lib for lib in manager.listThirdPartyLibs() if lib != 'libc++']
		
		print('Removing any previous versions of generated wrapper packages for {}...'.format(channel))
		_run(['conan', 'remove', '--force', '*/ue4@adamrehn/{}'.format(channel)])
		
		# Generate the package for each UE4-bundled thirdparty library
		for lib in libs:
			print('Generating and installing wrapper package for {}...'.format(lib))
			_generateWrapper(lib, template, delegates, tempDir, channel, profile)
		print('Done.')