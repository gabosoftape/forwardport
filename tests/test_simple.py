# -*- coding: utf-8 -*-
import collections
from datetime import datetime, timedelta
import time
from operator import itemgetter

import pytest

from utils import *

FAKE_PREV_WEEK = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')

# need:
# * an odoo server
#   - connected to a database
#   - with relevant modules loaded / installed
#   - set up project
#   - add repo, branch(es)
#   - provide visibility to contents si it can be queried & al
# * a tunnel so the server is visible from the outside (webhooks)
# * the ability to create repos on github
#   - repo name
#   - a github user to create a repo with
#   - a github owner to create a repo *for*
#   - provide ability to create commits, branches, prs, ...
def make_basic(env, config, make_repo, *, reponame='proj', project_name='myproject'):
    """ Creates a basic repo with 3 forking branches

    0 -- 1 -- 2 -- 3 -- 4  : a
              |
              `-- 11 -- 22 : b
                  |
                  `-- 111  : c
    each branch just adds and modifies a file (resp. f, g and h) through the
    contents sequence a b c d e
    """
    Projects = env['runbot_merge.project']
    project = Projects.search([('name', '=', project_name)])
    if not project:
        project = env['runbot_merge.project'].create({
            'name': project_name,
            'github_token': config['github']['token'],
            'github_prefix': 'hansen',
            'fp_github_token': config['github']['token'],
            'required_statuses': 'legal/cla,ci/runbot',
            'branch_ids': [
                (0, 0, {'name': 'a', 'fp_sequence': 2, 'fp_target': True}),
                (0, 0, {'name': 'b', 'fp_sequence': 1, 'fp_target': True}),
                (0, 0, {'name': 'c', 'fp_sequence': 0, 'fp_target': True}),
            ],
        })

    prod = make_repo(reponame)
    with prod:
        a_0, a_1, a_2, a_3, a_4, = prod.make_commits(
            None,
            Commit("0", tree={'f': 'a'}),
            Commit("1", tree={'f': 'b'}),
            Commit("2", tree={'f': 'c'}),
            Commit("3", tree={'f': 'd'}),
            Commit("4", tree={'f': 'e'}),
            ref='heads/a',
        )
        b_1, b_2 = prod.make_commits(
            a_2,
            Commit('11', tree={'g': 'a'}),
            Commit('22', tree={'g': 'b'}),
            ref='heads/b',
        )
        prod.make_commits(
            b_1,
            Commit('111', tree={'h': 'a'}),
            ref='heads/c',
        )
    other = prod.fork()
    project.write({
        'repo_ids': [(0, 0, {
            'name': prod.name,
            'fp_remote_target': other.name,
        })],
    })

    return prod, other
def test_straightforward_flow(env, config, make_repo, users):
    # TODO: ~all relevant data in users when creating partners
    # get reviewer's name
    reviewer_name = env['res.partner'].search([
        ('github_login', '=', users['reviewer'])
    ]).name

    prod, other = make_basic(env, config, make_repo)
    other_user = config['role_other']
    other_user_repo = prod.fork(token=other_user['token'])

    project = env['runbot_merge.project'].search([])
    b_head = prod.commit('b')
    c_head = prod.commit('c')
    with prod, other_user_repo:
        # create PR as a user with no access to prod (or other)
        [_, p_1] = other_user_repo.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            Commit('p_1', tree={'x': '1'}),
            ref='heads/hugechange'
        )
        pr = prod.make_pr(
            target='a', title="super important change",
            head=other_user['user'] + ':hugechange',
            token=other_user['token']
        )
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        # use rebase-ff (instead of rebase-merge) so we don't have to dig in
        # parents of the merge commit to find the cherrypicks
        pr.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    # should merge the staging then create the FP PR
    env.run_crons()

    p_1_merged = prod.commit('a')

    assert p_1_merged.id != p_1
    assert p_1_merged.message == MESSAGE_TEMPLATE.format(
        message='p_1',
        repo=prod.name,
        number=pr.number,
        headers='',
        name=reviewer_name,
        login=users['reviewer'],
    )
    assert prod.read_tree(p_1_merged) == {
        'f': 'e',
        'x': '1',
    }, "ensure p_1_merged has ~ the same contents as p_1 but is a different commit"
    [p_0_merged] = p_1_merged.parents

    # wait a bit for PR webhook... ?
    time.sleep(5)
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0.number == pr.number
    # 50 lines in, we can start checking the forward port...
    assert pr1.parent_id == pr0
    assert pr1.source_id == pr0
    other_owner = other.name.split('/')[0]
    assert re.match(other_owner + ':' + REF_PATTERN.format(target='b', source='hugechange'), pr1.label), \
        "check that FP PR was created in FP target repo"
    c = prod.commit(pr1.head)
    # TODO: add original committer (if !author) as co-author in commit message?
    assert c.author['name'] == other_user['user'], "author should still be original's probably"
    assert itemgetter('name', 'email')(c.committer) == (project.fp_github_name, project.fp_github_email)
    assert prod.read_tree(c) == {
        'f': 'c',
        'g': 'b',
        'x': '1'
    }
    with prod:
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        prod.post_status(pr1.head, 'success', 'legal/cla')

    env.run_crons()
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})

    assert pr.comments == [
        (users['reviewer'], 'hansen r+ rebase-ff'),
        (users['user'], 'Merge method set to rebase and fast-forward'),
        (users['user'], re_matches(r'Merged at [0-9a-f]{40}, thanks!')),
        (users['user'], 'This pull request has forward-port PRs awaiting action'),
    ]

    pr0_, pr1_, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0_ == pr0
    assert pr1_ == pr1
    assert pr2.parent_id == pr1
    assert pr2.source_id == pr0
    assert not pr0.squash, "original PR has >1 commit"
    assert not (pr1.squash or pr2.squash), "forward ports should also have >1 commit"
    assert re.match(REF_PATTERN.format(target='c', source='hugechange'), pr2.refname), \
        "check that FP PR was created in FP target repo"
    assert prod.read_tree(prod.commit(pr2.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '1'
    }
    assert prod.get_pr(pr2.number).comments == [
        (users['user'], """\
Ping @%s, @%s
This PR targets c and is the last of the forward-port chain.

To merge the full chain, say
> @%s r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (users['other'], users['reviewer'], project.fp_github_name)),
    ]
    with prod:
        prod.post_status(pr2.head, 'success', 'ci/runbot')
        prod.post_status(pr2.head, 'success', 'legal/cla')

        prod.get_pr(pr2.number).post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])

    env.run_crons()

    assert pr1.staging_id
    assert pr2.staging_id
    # two branches so should have two stagings
    assert pr1.staging_id != pr2.staging_id
    # validate
    with prod:
        prod.post_status('staging.b', 'success', 'ci/runbot')
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.c', 'success', 'ci/runbot')
        prod.post_status('staging.c', 'success', 'legal/cla')

    # and trigger merge
    env.run_crons()

    # apparently github strips out trailing newlines when fetching through the
    # API...
    message_template = MESSAGE_TEMPLATE.format(
        message='p_1',
        repo=prod.name,
        number='%s',
        headers='X-original-commit: {}\n'.format(p_1_merged.id),
        name=reviewer_name,
        login=users['reviewer'],
    )

    old_b = prod.read_tree(b_head)
    head_b = prod.commit('b')
    assert head_b.message == message_template % pr1.number
    assert prod.commit(head_b.parents[0]).message == 'p_0\n\nX-original-commit: %s' % p_0_merged
    b_tree = prod.read_tree(head_b)
    assert b_tree == {
        **old_b,
        'x': '1',
    }
    old_c = prod.read_tree(c_head)
    head_c = prod.commit('c')
    assert head_c.message == message_template % pr2.number
    assert prod.commit(head_c.parents[0]).message == 'p_0\n\nX-original-commit: %s' % p_0_merged
    c_tree = prod.read_tree(head_c)
    assert c_tree == {
        **old_c,
        'x': '1',
    }
    # check that we didn't just smash the original trees
    assert prod.read_tree(prod.commit('a')) != b_tree != c_tree

def test_update_pr(env, config, make_repo, users):
    """ Even for successful cherrypicks, it's possible that e.g. CI doesn't
    pass or the reviewer finds out they need to update the code.

    In this case, all following forward ports should... be detached? Or maybe
    only this one and its dependent should be updated?
    """
    prod, other = make_basic(env, config, make_repo)
    with prod:
        [p_1] = prod.make_commits(
            'a',
            Commit('p_0', tree={'x': '0'}),
            ref='heads/hugechange'
        )
        pr = prod.make_pr(target='a', head='hugechange')
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    # should merge the staging then create the FP PR
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')

    fp_intermediate = (users['user'], '''\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
''')
    ci_warning = (users['user'], 'Ping @%(user)s, @%(reviewer)s\n\nCI failed on this forward-port PR' % users)

    # oh no CI of the first FP PR failed!
    with prod:
        prod.post_status(pr1.head, 'failure', 'ci/runbot')
        prod.post_status(pr1.head, 'success', 'legal/cla')
    env.run_crons()
    # check that FP did not resume & we have a ping on the PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == pr0 | pr1,\
        "forward port should not continue on CI failure"
    pr1_remote = prod.get_pr(pr1.number)
    assert pr1_remote.comments == [fp_intermediate, ci_warning]

    # it was a false positive, rebuild... it fails again!
    with prod:
        prod.post_status(pr1.head, 'failure', 'ci/runbot')
    env.run_crons()
    # check that FP did not resume & we have a ping on the PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == pr0 | pr1,\
        "ensure it still hasn't restarted"
    assert pr1_remote.comments == [fp_intermediate, ci_warning, ci_warning]

    # nb: updating the head would detach the PR and not put it in the warning
    # path anymore

    # rebuild again, finally passes
    with prod:
        prod.post_status(pr1.head, 'success', 'ci/runbot')
    env.run_crons()

    pr0, pr1, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr1.parent_id == pr0
    assert pr2.parent_id == pr1
    pr1_head = pr1.head
    pr2_head = pr2.head

    # turns out branch b is syntactically but not semantically compatible! It
    # needs x to be 5!
    pr_repo, pr_ref = prod.get_pr(pr1.number).branch
    with pr_repo:
        # force-push correct commit to PR's branch
        [new_c] = pr_repo.make_commits(
            pr1.target.name,
            Commit('whop whop', tree={'x': '5'}),
            ref='heads/%s' % pr_ref
        )
    env.run_crons()

    assert pr1.head == new_c != pr1_head, "the FP PR should be updated"
    assert not pr1.parent_id, "the FP PR should be detached from the original"
    # NOTE: should the followup PR wait for pr1 CI or not?
    assert pr2.head != pr2_head
    assert pr2.parent_id == pr1, "the followup PR should still be linked"

    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '5'
    }, "the FP PR should have the new code"
    assert prod.read_tree(prod.commit(pr2.head)) == {
        'f': 'c',
        'g': 'a',
        'h': 'a',
        'x': '5'
    }, "the followup FP should also have the update"

    with pr_repo:
        pr_repo.make_commits(
            pr1.target.name,
            Commit('fire!', tree={'h': '0'}),
            ref='heads/%s' % pr_ref,
        )
    env.run_crons()
    # since there are PRs, this is going to update pr2 as broken
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'c',
        'g': 'b',
        'h': '0'
    }
    assert prod.read_tree(prod.commit(pr2.head)) == {
        'f': 'c',
        'g': 'a',
        'h': re_matches(r'''<<<<<<< HEAD
a
=======
0
>>>>>>> [0-9a-f]{7,}(...)? temp
'''),
    }

def test_conflict(env, config, make_repo):
    """ If there's a conflict when forward-porting the commit, commit the
    conflict and create a draft PR.
    """
    prod, other = make_basic(env, config, make_repo)
    # reset b to b~1 (g=a) parent so there's no b -> c conflict
    with prod:
        prod.update_ref('heads/b', prod.commit('b').parents[0], force=True)

    # generate a conflict: create a g file in a PR to a
    with prod:
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'g': 'xxx'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    # wait a bit for PR webhook... ?
    time.sleep(5)
    env.run_crons()

    # should have created a new PR
    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    # but it should not have a parent, and there should be conflict markers
    assert not pr1.parent_id
    assert pr1.source_id == pr0
    assert prod.read_tree(prod.commit('b')) == {
        'f': 'c',
        'g': 'a',
    }
    assert pr1.state == 'opened'
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'c',
        'g': re_matches(r'''<<<<<<< HEAD
a
=======
xxx
>>>>>>> [0-9a-f]{7,}(...)? temp
'''),
    }

    # check that CI passing does not create more PRs
    with prod:
        validate_all([prod], [pr1.head])
    env.run_crons()
    time.sleep(5)
    env.run_crons()
    assert pr0 | pr1 == env['runbot_merge.pull_requests'].search([], order='number'),\
        "CI passing should not have resumed the FP process on a conflicting / draft PR"

    # fix the PR, should behave as if this were a normal PR
    get_pr = prod.get_pr(pr1.number)
    pr_repo, pr_ref = get_pr.branch
    with pr_repo:
        pr_repo.make_commits(
            # if just given a branch name, goes and gets it from pr_repo whose
            # "b" was cloned before that branch got rolled back
            prod.commit('b').id,
            Commit('g should indeed b xxx', tree={'g': 'xxx'}),
            ref='heads/%s' % pr_ref
        )
    env.run_crons()
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'c',
        'g': 'xxx',
    }
    assert pr1.state == 'opened', "state should be open still"

    # check that merging the fixed PR fixes the flow and restarts a forward
    # port process
    with prod:
        prod.post_status(pr1.head, 'success', 'legal/cla')
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        get_pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert pr1.staging_id
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    *_, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr2.parent_id == pr1
    assert pr2.source_id == pr0
    assert re.match(
        REF_PATTERN.format(target='c', source='conflicting'),
        pr2.refname
    )
    assert prod.read_tree(prod.commit(pr2.head)) == {
        'f': 'c',
        'g': 'xxx',
        'h': 'a',
    }

def test_conflict_deleted(env, config, make_repo):
    prod, other = make_basic(env, config, make_repo)
    # remove f from b
    with prod:
        prod.make_commits(
            'b', Commit('33', tree={'g': 'c'}, reset=True),
            ref='heads/b'
        )

    # generate a conflict: update f in a
    with prod:
        [p_0] = prod.make_commits(
            'a', Commit('p_0', tree={'f': 'xxx'}),
            ref='heads/conflicting'
        )
        pr = prod.make_pr(target='a', head='conflicting')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    # wait a bit for PR webhook... ?
    time.sleep(5)
    env.run_crons()

    # should have created a new PR
    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    # but it should not have a parent
    assert not pr1.parent_id
    assert pr1.source_id == pr0
    assert prod.read_tree(prod.commit('b')) == {
        'g': 'c',
    }
    assert pr1.state == 'opened'
    # NOTE: no actual conflict markers because pr1 essentially adds f de-novo
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'f': 'xxx',
        'g': 'c',
    }

    # check that CI passing does not create more PRs
    with prod:
        validate_all([prod], [pr1.head])
    env.run_crons()
    time.sleep(5)
    env.run_crons()
    assert pr0 | pr1 == env['runbot_merge.pull_requests'].search([], order='number'),\
        "CI passing should not have resumed the FP process on a conflicting / draft PR"

    # fix the PR, should behave as if this were a normal PR
    get_pr = prod.get_pr(pr1.number)
    pr_repo, pr_ref = get_pr.branch
    with pr_repo:
        pr_repo.make_commits(
            # if just given a branch name, goes and gets it from pr_repo whose
            # "b" was cloned before that branch got rolled back
            prod.commit('b').id,
            Commit('f should indeed be removed', tree={'g': 'c'}, reset=True),
            ref='heads/%s' % pr_ref
        )
    env.run_crons()
    assert prod.read_tree(prod.commit(pr1.head)) == {
        'g': 'c',
    }
    assert pr1.state == 'opened', "state should be open still"

def test_empty(env, config, make_repo, users):
    """ Cherrypick of an already cherrypicked (or separately implemented)
    commit -> create draft PR.
    """
    prod, other = make_basic(env, config, make_repo)
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')

    # merge same change to a afterwards
    with prod:
        [p_1] = prod.make_commits(
            'a', Commit('p_0', tree={'x': '0'}),
            ref='heads/late'
        )
        pr1 = prod.make_pr(target='a', head='late')
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    assert prod.read_tree(prod.commit('a')) == {
        'f': 'e',
        'x': '0',
    }
    assert prod.read_tree(prod.commit('b')) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }
    # should have 4 PRs:
    # PR 0
    # FP of PR 0 to C
    # PR 1
    # failed FP of PR1 to B
    prs = env['runbot_merge.pull_requests'].search([], order='number')
    assert len(prs) == 4
    pr0_id = prs.filtered(lambda p: p.number == pr0.number)
    pr1_id = prs.filtered(lambda p: p.number == pr1.number)
    fp_id = prs.filtered(lambda p: p.parent_id == pr0_id)
    fail_id = prs - (pr0_id | pr1_id | fp_id)
    assert fp_id
    assert fail_id
    # unlinked from parent since cherrypick failed
    assert not fail_id.parent_id
    # the tree should be clean...
    assert prod.read_tree(prod.commit(fail_id.head)) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }

    with prod:
        validate_all([prod], [fp_id.head, fail_id.head])
    env.run_crons()

    # should not have created any new PR
    assert env['runbot_merge.pull_requests'].search([], order='number') == prs

    # check reminder
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})

    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], re_matches(r'Merged at [0-9a-f]{40}, thanks!')),
        (users['user'], 'This pull request has forward-port PRs awaiting action'),
        (users['user'], 'This pull request has forward-port PRs awaiting action'),
    ], "each cron run should trigger a new message on the ancestor"
    # check that this stops if we close the PR
    with prod:
        prod.get_pr(fail_id.number).close()
    env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron', context={'forwardport_updated_before': FAKE_PREV_WEEK})
    assert pr1.comments == [
        (users['reviewer'], 'hansen r+'),
        (users['user'], re_matches(r'Merged at [0-9a-f]{40}, thanks!')),
        (users['user'], 'This pull request has forward-port PRs awaiting action'),
        (users['user'], 'This pull request has forward-port PRs awaiting action'),
    ]

def test_partially_empty(env, config, make_repo):
    """ Check what happens when only some commits of the PR are now empty
    """
    prod, other = make_basic(env, config, make_repo)
    # merge change to b
    with prod:
        [p_0] = prod.make_commits(
            'b', Commit('p', tree={'x': '0'}),
            ref='heads/early'
        )
        pr0 = prod.make_pr(target='b', head='early')
        prod.post_status(p_0, 'success', 'legal/cla')
        prod.post_status(p_0, 'success', 'ci/runbot')
        pr0.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')

    # merge same change to a afterwards
    with prod:
        [*_, p_1] = prod.make_commits(
            'a',
            Commit('p_0', tree={'w': '0'}),
            Commit('p_1', tree={'x': '0'}),
            Commit('p_2', tree={'y': '0'}),
            ref='heads/late'
        )
        pr1 = prod.make_pr(target='a', head='late')
        prod.post_status(p_1, 'success', 'legal/cla')
        prod.post_status(p_1, 'success', 'ci/runbot')
        pr1.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')

    env.run_crons()
    assert prod.read_tree(prod.commit('a')) == {
        'f': 'e',
        'w': '0',
        'x': '0',
        'y': '0',
    }
    assert prod.read_tree(prod.commit('b')) == {
        'f': 'c',
        'g': 'b',
        'x': '0',
    }

    fail_id = env['runbot_merge.pull_requests'].search([
        ('number', 'not in', [pr0.number, pr1.number]),
        ('parent_id', '=', False),
    ], order='number')
    assert fail_id
    # unlinked from parent since cherrypick failed
    assert not fail_id.parent_id
    # the tree should be clean...
    assert prod.read_tree(prod.commit(fail_id.head)) == {
        'f': 'c',
        'g': 'b',
        'w': '0',
        'x': '0',
        'y': '0',
    }

Description = collections.namedtuple('Restriction', 'source limit')
def test_limit_configure(env, config, make_repo):
    """ Checks that configuring an FP limit on a PR is respected

    * limits to not the latest
    * limits to the current target (= no FP)
    * limits to an earlier branch (???)
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    descriptions = [
        Description(source='a', limit='b'),
        Description(source='b', limit='b'),
        Description(source='b', limit='a'),
    ]
    originals = []
    with prod:
        for i, descr in enumerate(descriptions):
            [c] = prod.make_commits(
                descr.source, Commit('c %d' % i, tree={str(i): str(i)}),
                ref='heads/branch%d' % i,
            )
            pr = prod.make_pr(target=descr.source, head='branch%d'%i)
            prod.post_status(c, 'success', 'legal/cla')
            prod.post_status(c, 'success', 'ci/runbot')
            pr.post_comment('hansen r+\n%s up to %s' % (bot_name, descr.limit), config['role_reviewer']['token'])
            originals.append(pr.number)
    env.run_crons()
    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
        prod.post_status('staging.b', 'success', 'legal/cla')
        prod.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    # should have created a single FP PR for 0, none for 1 and none for 2
    prs = env['runbot_merge.pull_requests'].search([], order='number')
    assert len(prs) == 4
    assert prs[-1].parent_id == prs[0]
    assert prs[0].number == originals[0]
    assert prs[1].number == originals[1]
    assert prs[2].number == originals[2]

@pytest.mark.parametrize('enabled', ['active', 'fp_target'])
def test_limit_disable(env, config, make_repo, users, enabled):
    """ Checks behaviour if the limit target is disabled:

    * disable target while FP is ongoing -> skip over (and stop there so no FP)
    * forward-port over a disabled branch
    * request a disabled target as limit

    Disabling (with respect to forward ports) can be performed by marking the
    branch as !active (which also affects mergebot operations), or as
    !fp_target (won't be forward-ported to).
    """
    prod, other = make_basic(env, config, make_repo)
    bot_name = env['runbot_merge.project'].search([]).fp_github_name
    with prod:
        [c] = prod.make_commits('a', Commit('c 0', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to b' % bot_name, config['role_reviewer']['token'])

        [c] = prod.make_commits('a', Commit('c 1', tree={'1': '1'}), ref='heads/branch1')
        pr = prod.make_pr(target='a', head='branch1')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    # disable branch b
    env['runbot_merge.branch'].search([('name', '=', 'b')]).write({enabled: False})
    env.run_crons()

    # should have created a single PR (to branch c, for pr 1)
    _0, _1, p = env['runbot_merge.pull_requests'].search([], order='number')
    assert p.parent_id == _1
    assert p.target.name == 'c'

    with prod:
        [c] = prod.make_commits('a', Commit('c 2', tree={'2': '2'}), ref='heads/branch2')
        pr = prod.make_pr(target='a', head='branch2')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+\n%s up to' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to b' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to foo' % bot_name, config['role_reviewer']['token'])
        pr.post_comment('%s up to c' % bot_name, config['role_reviewer']['token'])
    env.run_crons()

    # use a set because git webhooks delays might lead to mis-ordered
    # responses and we don't care that much
    assert set(pr.comments) == {
        (users['reviewer'], "hansen r+\n%s up to" % bot_name),
        (users['reviewer'], "%s up to b" % bot_name),
        (users['reviewer'], "%s up to foo" % bot_name),
        (users['reviewer'], "%s up to c" % bot_name),
        (users['user'], "Please provide a branch to forward-port to."),
        (users['user'], "Branch 'b' is disabled, it can't be used as a forward port target."),
        (users['user'], "There is no branch 'foo', it can't be used as a forward port target."),
        (users['user'], "Forward-porting to 'c'."),
    }

def test_default_disabled(env, config, make_repo, users):
    """ If the default limit is disabled, it should still be the default
    limit but the ping message should be set on the actual last FP (to the
    last non-deactivated target)
    """
    prod, other = make_basic(env, config, make_repo)
    branch_c = env['runbot_merge.branch'].search([('name', '=', 'c')])
    branch_c.fp_target = False

    with prod:
        [c] = prod.make_commits('a', Commit('c', tree={'0': '0'}), ref='heads/branch0')
        pr = prod.make_pr(target='a', head='branch0')
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search([]).limit_id == branch_c

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    p1, p2 = env['runbot_merge.pull_requests'].search([], order='number')
    assert p1.number == pr.number
    pr2 = prod.get_pr(p2.number)

    cs = pr2.comments
    assert len(cs) == 1
    assert pr2.comments == [
        (users['user'], """\
Ping @%s, @%s
This PR targets b and is the last of the forward-port chain.

To merge the full chain, say
> @%s r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""" % (users['user'], users['reviewer'], users['user'])),
    ]

# reviewer = of the FP sequence, the original PR is always reviewed by `user`
# set as reviewer
Case = collections.namedtuple('Case', 'author reviewer delegate success')
ACL = [
    Case('reviewer', 'reviewer', None, True),
    Case('reviewer', 'self_reviewer', None, False),
    Case('reviewer', 'other', None, False),
    Case('reviewer', 'other', 'other', True),

    Case('self_reviewer', 'reviewer', None, True),
    Case('self_reviewer', 'self_reviewer', None, True),
    Case('self_reviewer', 'other', None, False),
    Case('self_reviewer', 'other', 'other', True),

    Case('other', 'reviewer', None, True),
    Case('other', 'self_reviewer', None, False),
    Case('other', 'other', None, True),
    Case('other', 'other', 'other', True),
]
@pytest.mark.parametrize(Case._fields, ACL)
def test_access_rights(env, config, make_repo, users, author, reviewer, delegate, success):
    prod, other = make_basic(env, config, make_repo)
    project = env['runbot_merge.project'].search([])

    # create a partner for `user`
    env['res.partner'].create({
        'name': users['user'],
        'github_login': users['user'],
        'reviewer': True,
    })

    author_token = config['role_' + author]['token']
    fork = prod.fork(token=author_token)
    with prod, fork:
        [c] = fork.make_commits('a', Commit('c_0', tree={'y': '0'}), ref='heads/accessrights')
        pr = prod.make_pr(
            target='a', title='my change',
            head=users[author] + ':accessrights',
            token=author_token,
        )
        prod.post_status(c, 'success', 'legal/cla')
        prod.post_status(c, 'success', 'ci/runbot')
        pr.post_comment('hansen r+', token=config['github']['token'])
        if delegate:
            pr.post_comment('hansen delegate=%s' % users[delegate], token=config['github']['token'])
    env.run_crons()

    with prod:
        prod.post_status('staging.a', 'success', 'legal/cla')
        prod.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
    assert pr0.state == 'merged'
    with prod:
        prod.post_status(pr1.head, 'success', 'ci/runbot')
        prod.post_status(pr1.head, 'success', 'legal/cla')
    env.run_crons()

    _, _, pr2 = env['runbot_merge.pull_requests'].search([], order='number')
    with prod:
        prod.post_status(pr2.head, 'success', 'ci/runbot')
        prod.post_status(pr2.head, 'success', 'legal/cla')
        prod.get_pr(pr2.number).post_comment(
            '%s r+' % project.fp_github_name,
            token=config['role_' + reviewer]['token']
        )
    env.run_crons()
    if success:
        assert pr1.staging_id and pr2.staging_id,\
            "%s should have approved FP of PRs by %s" % (reviewer, author)
    else:
        assert not (pr1.staging_id or pr2.staging_id),\
            "%s should *not* have approved FP of PRs by %s" % (reviewer, author)

def test_batched(env, config, make_repo, users):
    """ Tests for projects with multiple repos & sync'd branches. Batches
    should be FP'd to batches
    """
    main1, _ = make_basic(env, config, make_repo, reponame='main1')
    main2, _ = make_basic(env, config, make_repo, reponame='main2')
    main1.unsubscribe(config['role_reviewer']['token'])
    main2.unsubscribe(config['role_reviewer']['token'])

    friendo = config['role_other']
    other1 = main1.fork(token=friendo['token'])
    other2 = main2.fork(token=friendo['token'])

    with main1, other1:
        [c1] = other1.make_commits(
            'a', Commit('commit repo 1', tree={'1': 'a'}),
            ref='heads/contribution'
        )
        pr1 = main1.make_pr(
            target='a', title="My contribution",
            head=friendo['user'] + ':contribution',
            token=friendo['token']
        )
        # we can ack it directly as it should not be taken in account until
        # we run crons
        validate_all([main1], [c1])
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with main2, other2:
        [c2] = other2.make_commits(
            'a', Commit('commit repo 2', tree={'2': 'a'}),
            ref='heads/contribution' # use same ref / label as pr1
        )
        pr2 = main2.make_pr(
            target='a', title="Main2 part of my contribution",
            head=friendo['user'] + ':contribution',
            token=friendo['token']
        )
        validate_all([main2], [c2])
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])

    env.run_crons()

    # sanity check: this should have created a staging with 1 batch with pr1 and pr2
    stagings = env['runbot_merge.stagings'].search([])
    assert len(stagings) == 1
    assert stagings.target.name == 'a'
    assert len(stagings.batch_ids) == 1
    assert stagings.mapped('batch_ids.prs.number') == [pr1.number, pr2.number]

    with main1, main2:
        validate_all([main1, main2], ['staging.a'])
    env.run_crons()

    PullRequests = env['runbot_merge.pull_requests']
    # created the first forward port, need to validate it so the second one is
    # triggered (FP only goes forward on CI+) (?)
    pr1b = PullRequests.search([
        ('source_id', '!=', False),
        ('repository.name', '=', main1.name),
    ])
    pr2b = PullRequests.search([
        ('source_id', '!=', False),
        ('repository.name', '=', main2.name),
    ])
    # check that relevant users were pinged
    ping = [
        (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
"""),
    ]
    pr_remote_1b = main1.get_pr(pr1b.number)
    pr_remote_2b = main2.get_pr(pr2b.number)
    assert pr_remote_1b.comments == ping
    assert pr_remote_2b.comments == ping

    with main1, main2:
        validate_all([main1], [pr1b.head])
        validate_all([main2], [pr2b.head])
    env.run_crons() # process updated statuses -> generate followup FP

    # should have created two PRs whose source is p1 and two whose source is p2
    pr1a, pr1b, pr1c = PullRequests.search([
        ('repository.name', '=', main1.name),
    ], order='number')
    pr2a, pr2b, pr2c = PullRequests.search([
        ('repository.name', '=', main2.name),
    ], order='number')

    assert pr1a.number == pr1.number
    assert pr2a.number == pr2.number
    assert pr1a.state == pr2a.state == 'merged'

    assert pr1b.label == pr2b.label, "batched source should yield batched FP"
    assert pr1c.label == pr2c.label, "batched source should yield batched FP"
    assert pr1b.label != pr1c.label

    project = env['runbot_merge.project'].search([])
    # ok main1 PRs
    with main1:
        validate_all([main1], [pr1c.head])
        main1.get_pr(pr1c.number).post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])
    env.run_crons()

    # check that the main1 PRs are ready but blocked on the main2 PRs
    assert pr1b.state == 'ready'
    assert pr1c.state == 'ready'
    assert pr1b.blocked
    assert pr1c.blocked

    # ok main2 PRs
    with main2:
        validate_all([main2], [pr2c.head])
        main2.get_pr(pr2c.number).post_comment('%s r+' % project.fp_github_name, config['role_reviewer']['token'])
    env.run_crons()

    stb, stc = env['runbot_merge.stagings'].search([], order='target')
    assert stb.target.name == 'b'
    assert stc.target.name == 'c'

    with main1, main2:
        validate_all([main1, main2], ['staging.b', 'staging.c'])

class TestClosing:
    def test_closing_before_fp(self, env, config, make_repo, users):
        """ Closing a PR should preclude its forward port
        """
        prod, other = make_basic(env, config, make_repo)
        with prod:
            [p_1] = prod.make_commits(
                'a',
                Commit('p_0', tree={'x': '0'}),
                ref='heads/hugechange'
            )
            pr = prod.make_pr(target='a', head='hugechange')
            prod.post_status(p_1, 'success', 'legal/cla')
            prod.post_status(p_1, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        with prod:
            prod.post_status('staging.a', 'success', 'legal/cla')
            prod.post_status('staging.a', 'success', 'ci/runbot')
        # should merge the staging then create the FP PR
        env.run_crons()

        pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        # close the FP PR then have CI validate it
        with prod:
            prod.get_pr(pr1.number).close()
        assert pr1.state == 'closed'
        assert not pr1.parent_id, "closed PR should should be detached from its parent"
        with prod:
            prod.post_status(pr1.head, 'success', 'legal/cla')
            prod.post_status(pr1.head, 'success', 'ci/runbot')
        env.run_crons()
        env.run_crons('forwardport.reminder', 'runbot_merge.feedback_cron')

        assert env['runbot_merge.pull_requests'].search([], order='number') == pr0 | pr1,\
            "closing the PR should suppress the FP sequence"
        assert prod.get_pr(pr1.number).comments == [
            (users['user'], """\
This PR targets b and is part of the forward-port chain. Further PRs will be created up to c.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""")
        ]

    def test_closing_after_fp(self, env, config, make_repo):
        """ Closing a PR which has been forward-ported should not touch the
        followups
        """
        prod, other = make_basic(env, config, make_repo)
        with prod:
            [p_1] = prod.make_commits(
                'a',
                Commit('p_0', tree={'x': '0'}),
                ref='heads/hugechange'
            )
            pr = prod.make_pr(target='a', head='hugechange')
            prod.post_status(p_1, 'success', 'legal/cla')
            prod.post_status(p_1, 'success', 'ci/runbot')
            pr.post_comment('hansen r+', config['role_reviewer']['token'])

        env.run_crons()
        with prod:
            prod.post_status('staging.a', 'success', 'legal/cla')
            prod.post_status('staging.a', 'success', 'ci/runbot')

        # should merge the staging then create the FP PR
        env.run_crons()

        pr0, pr1 = env['runbot_merge.pull_requests'].search([], order='number')
        with prod:
            prod.post_status(pr1.head, 'success', 'legal/cla')
            prod.post_status(pr1.head, 'success', 'ci/runbot')
        # should create the second staging
        env.run_crons()

        pr0_1, pr1_1, pr2_1 = env['runbot_merge.pull_requests'].search([], order='number')
        assert pr0_1 == pr0
        assert pr1_1 == pr1

        with prod:
            prod.get_pr(pr1.number).close()

        assert pr1_1.state == 'closed'
        assert not pr1_1.parent_id
        assert pr2_1.state == 'opened'

def sPeNgBaB(s):
    return ''.join(
        l if i % 2 == 0 else l.upper()
        for i, l in enumerate(s)
    )
def test_spengbab():
    assert sPeNgBaB("spongebob") == 'sPoNgEbOb'

class TestRecognizeCommands:
    def make_pr(self, env, config, make_repo):
        r, _ = make_basic(env, config, make_repo)

        with r:
            r.make_commits('c', Commit('p', tree={'x': '0'}), ref='heads/testbranch')
            pr = r.make_pr(target='a', head='testbranch')

        return r, pr, env['runbot_merge.pull_requests'].search([
            ('repository.name', '=', r.name),
            ('number', '=', pr.number),
        ])

    def test_botname_casing(self, env, config, make_repo):
        """ Test that the botname is case-insensitive as people might write
        bot names capitalised or titlecased or uppercased or whatever
        """
        repo, pr, pr_id = self.make_pr(env, config, make_repo)
        assert pr_id.state == 'opened'
        botname = env['runbot_merge.project'].search([]).fp_github_name
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])
        [c] = env['runbot_merge.branch'].search([
            ('name', '=', 'c')
        ])

        names = [
            botname,
            botname.upper(),
            botname.capitalize(),
            sPeNgBaB(botname),
        ]

        for n in names:
            assert pr_id.limit_id == c
            with repo:
                pr.post_comment('@%s up to a' % n, config['role_reviewer']['token'])
            assert pr_id.limit_id == a
            # reset state
            pr_id.write({'limit_id': c.id})

    @pytest.mark.parametrize('indent', ['', '\N{SPACE}', '\N{SPACE}'*4, '\N{TAB}'])
    def test_botname_indented(self, env, config, make_repo, indent):
        """ matching botname should ignore leading whitespaces
        """
        repo, pr, pr_id = self.make_pr(env, config, make_repo)
        assert pr_id.state == 'opened'
        botname = env['runbot_merge.project'].search([]).fp_github_name
        [a] = env['runbot_merge.branch'].search([
            ('name', '=', 'a')
        ])
        [c] = env['runbot_merge.branch'].search([
            ('name', '=', 'c')
        ])

        assert pr_id.limit_id == c
        with repo:
            pr.post_comment('%s@%s up to a' % (indent, botname), config['role_reviewer']['token'])
        assert pr_id.limit_id == a
