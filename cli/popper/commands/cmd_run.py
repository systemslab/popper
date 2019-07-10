import os
import re
import sys

import click

import popper.cli
from popper.cli import pass_context, log
from popper.gha import WorkflowRunner
from popper.parser import Workflow
from popper import utils as pu, scm
from popper import log as logging


@click.command(
    'run', short_help='Run a workflow or action.')
@click.argument(
    'target',
    required=False
)
@click.option(
    '--debug',
    help=(
        'Generate detailed messages of what popper does (overrides --quiet)'),
    required=False,
    is_flag=True
)
@click.option(
    '--dry-run',
    help='Do not run the workflow, only print what would be executed.',
    required=False,
    is_flag=True
)
@click.option(
    '--log-file',
    help='Path to a log file. No log is created if this is not given.',
    required=False
)
@click.option(
    '--on-failure',
    help='Run the given action if there is a failure.',
    required=False
)
@click.option(
    '--parallel',
    help='Executes actions in stages in parallel.',
    required=False,
    is_flag=True
)
@click.option(
    '--quiet',
    help='Do not print output generated by actions.',
    required=False,
    is_flag=True
)
@click.option(
    '--recursive',
    help=(
        'Run any .workflow file found recursively from current path. '
        'Ignores flags --on-failure, --skip and --with-dependencies.'
    ),
    required=False,
    is_flag=True
)
@click.option(
    '--reuse',
    help='Reuse containers between executions (persist container state).',
    required=False,
    is_flag=True,
)
@click.option(
    '--runtime',
    help='Specify runtime for executing the workflow [default: docker].',
    type=click.Choice(['docker', 'singularity']),
    required=False,
    default='docker'
)
@click.option(
    '--skip',
    help=('Skip the given action (can be given multiple times).'),
    required=False,
    default=list(),
    multiple=True
)
@click.option(
    '--skip-clone',
    help='Skip pulling container images (assume they exist in local cache).',
    required=False,
    is_flag=True
)
@click.option(
    '--skip-pull',
    help='Skip cloning action repositories (assume they have been cloned).',
    required=False,
    is_flag=True
)
@click.option(
    '--with-dependencies',
    help=(
        'When an action argument is given (first positional argument), '
        'execute all its dependencies as well.'
    ),
    required=False,
    is_flag=True
)
@click.option(
    '--workspace',
    help='Path to workspace folder.',
    required=False,
    show_default=False,
    hidden=True,
    default=popper.scm.get_git_root_folder()
)
@pass_context
def cli(ctx, **kwargs):
    """Executes one or more workflows and reports on their status.

    [TARGET] : It can be either path to a workflow file or an action name.
    If TARGET is a workflow, the workflow is executed.
    If TARGET is an action, the specified action from the default workflow
    will be executed.

    Examples:

    1. When no TARGET argument is passed, Popper will search for the
    default workflow (.github/main.workflow or main.workflow) and
    execute it if found.

       $ popper run

    2. When workflow file is passed as arguments, the specified workflow
    will be executed.

       $ popper run /path/to/file.workflow

    3. When an action name is passed as argument, Popper will search for
    the action in the default workflow and if found, only the action
    will be executed.

       $ popper run myaction

    Note:

    * An action argument or options that take action as argument
    is not supported in recursive mode.

    * When CI is set, popper run searches for special keywords of the form
    `popper:run[...]`. If found, popper executes with the options given in
    these run instances else popper executes all the workflows recursively.
    """
    if os.environ.get('CI') == 'true':
        # When CI is set,
        log.info('Running in CI environment...')
        popper_run_instances = parse_commit_message()
        if popper_run_instances:
            for args in get_args(popper_run_instances):
                kwargs.update(args)
                if kwargs['recursive']:
                    log.warn('When CI is set, --recursive is ignored.')
                    kwargs['recursive'] = False
                prepare_workflow_execution(**kwargs)
        else:
            # If no special keyword is found, we run all the workflows,
            # recursively.
            kwargs['recursive'] = True
            prepare_workflow_execution(**kwargs)
    else:
        # When CI is not set,
        prepare_workflow_execution(**kwargs)


def prepare_workflow_execution(**kwargs):
    """Set parameters for the workflow execution
    and run the workflow."""

    def inspect_target(target):
        if target:
            if target.endswith('.workflow'):
                return pu.find_default_wfile(target), None
            else:
                return pu.find_default_wfile(), target
        else:
            return pu.find_default_wfile(), target

    # Set the logging levels.
    level = 'ACTION_INFO'
    if kwargs['quiet']:
        level = 'INFO'
    if kwargs['debug']:
        level = 'DEBUG'
    log.setLevel(level)
    if kwargs['log_file']:
        logging.add_log(log, kwargs['log_file'])

    # Remove the unnecessary kwargs.
    kwargs.pop('quiet')
    kwargs.pop('debug')
    kwargs.pop('log_file')

    # Run the workflow accordingly as recursive/CI and Non-CI.
    recursive = kwargs.pop('recursive')
    target = kwargs.pop('target')
    with_dependencies = kwargs['with_dependencies']
    skip = kwargs['skip']
    on_failure = kwargs['on_failure']

    if recursive:
        if target or with_dependencies or skip or on_failure:
            # In recursive mode, these flags cannot be used.
            log.fail('Any combination of [target] argument, '
                     '--with-dependencies <action>, --skip <action>, '
                     '--on-failure <action> is invalid in recursive mode.')

        for wfile in pu.find_recursive_wfile():
            run_workflow(wfile, target, **kwargs)
    else:
        wfile, action = inspect_target(target)
        run_workflow(wfile, action, **kwargs)


def run_workflow(wfile, action, **kwargs):

    log.info('Found and running workflow at ' + wfile)
    # Initialize a Worklow. During initialization all the validation
    # takes place automatically.
    wf = Workflow(wfile)
    wf_runner = WorkflowRunner(wf)

    # Saving workflow instance for signal handling
    popper.cli.interrupt_params['parallel'] = kwargs['parallel']

    if kwargs['parallel']:
        if sys.version_info[0] < 3:
            log.fail('--parallel is only supported on Python3')
        log.warn("Using --parallel may result in interleaved output. "
                 "You may use --quiet flag to avoid confusion.")

    if kwargs['with_dependencies'] and (not action):
        log.fail('`--with-dependencies` can be used only with '
                 'action argument.')

    if kwargs['skip'] and action:
        log.fail('`--skip` can\'t be used when action argument '
                 'is passed.')

    on_failure = kwargs.pop('on_failure')

    try:
        wf_runner.run(action, **kwargs)
    except SystemExit as e:
        if (e.code != 0) and on_failure:
            kwargs['skip'] = list()
            action = on_failure
            wf_runner.run(action, **kwargs)
        else:
            raise

    if action:
        log.info('Action "{}" finished successfully.'.format(action))
    else:
        log.info('Workflow "{}" finished successfully.'.format(wfile))


def parse_commit_message():
    """Parse `popper:run[]` keywords from head commit message.
    """
    head_commit = scm.get_head_commit()
    if not head_commit:
        return None

    msg = head_commit.message
    if 'Merge' in msg:
        log.info("Merge detected. Reading message from merged commit.")
        if len(head_commit.parents) == 2:
            msg = head_commit.parents[1].message

    if 'popper:run[' not in msg:
        return None

    pattern = r'popper:run\[(.+?)\]'
    popper_run_instances = re.findall(pattern, msg)
    return popper_run_instances


def get_args(popper_run_instances):
    """Parse the argument strings from popper:run[..] instances
    and return the args."""
    for args in popper_run_instances:
        args = args.split(" ")
        ci_context = cli.make_context('popper run', args)
        yield ci_context.params
