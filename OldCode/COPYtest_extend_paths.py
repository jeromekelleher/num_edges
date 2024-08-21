import msprime
import numpy as np
import pytest

import _tskit
# import tests.test_wright_fisher as wf
import tskit
import COPYtsutil as tsutil
# from tests.test_highlevel import get_example_tree_sequences

# ↑ See https://github.com/tskit-dev/tskit/issues/1804 for when
# we can remove this.


def _slide_mutation_nodes_up(ts, mutations):
    # adjusts mutations' nodes to place each mutation on the correct edge given
    # their time; requires mutation times be nonmissing and the mutation times
    # be >= their nodes' times.

    assert np.all(~tskit.is_unknown_time(mutations.time)), "times must be known"
    new_nodes = mutations.node.copy()

    mut = 0
    for tree in ts.trees():
        _, right = tree.interval
        while (
            mut < mutations.num_rows and ts.sites_position[mutations.site[mut]] < right
        ):
            t = mutations.time[mut]
            c = mutations.node[mut]
            p = tree.parent(c)
            assert ts.nodes_time[c] <= t
            while p != -1 and ts.nodes_time[p] <= t:
                c = p
                p = tree.parent(c)
            assert ts.nodes_time[c] <= t
            if p != -1:
                assert t < ts.nodes_time[p]
            new_nodes[mut] = c
            mut += 1

    # in C the node column can be edited in place
    new_mutations = mutations.copy()
    new_mutations.clear()
    for mut, n in zip(mutations, new_nodes):
        new_mutations.append(mut.replace(node=n))

    return new_mutations


"""
Here is our new `extend_paths` algorithm.
This handles some other tricker cases we want the
`extend_edges` algorithm to succeed on.
This algorithm can also extend edges, however
its convergence is now not monotonically decreasing;
this makes convergence INCREDIBLY SLOW.
We think that we can combine `extend_paths` and `extend_edges`
in a piece-meal way to speed up this convergence, but this requires
further testing.
"""


class PathExtender:
    def __init__(self, ts, forwards):
        """
        Below we will iterate through the trees, either to the left or the right,
        keeping the following state consistent:
        - we are moving from a previous tree, last_tree, to new one, next_tree
        - here: the position that separates the last_tree from the next_tree
        - (here, there): the segment covered by next_tree
        - edges_out: edges to be removed from last_tree to get next_tree
        - parent_out: the forest induced by edges_out, a subset of last_tree
        - edges_in: edges to be added to last_tree to get next_tree
        - parent_in: the forest induced by edges_in, a subset of next_tree
        - next_degree: the negree of each node in next_tree
        - next_nodes_edge: for each node, the edge above it in next_tree
        - last_degree: the negree of each node in last_tree
        - last_nodes_edge: for each node, the edge above it in last_tree
        Except: each of edges_in and edges_out is of the form e, x, and the
        label x>0 if the edge is postponed to the next segment.
        The label is x=1 for postponed edges, and x=2 for new edges.
        In other words:
        - elements e, x of edges_out with x=0 are in last_tree but not next_tree
        - elements e, x of edges_in with x=0 are in next_tree but not last_tree
        - elements e, x of edges_out with x=1 are in both trees,
            and hence don't count for parent_out
        - elements e, x of edges_in with x=1 are in neither,
            and hence don't count for parent_in
        - elements e, x for edges_out with x=2 have just been added, and so ought
            to count towards the next tree, but we have to put them in edges out
            because they'll be removed next time.
        Notes:
        - things having to do with last_tree do not change,
          but things having to do with next_tree might change as we go along
        - parent_out and parent_in do not refer to the *entire* last/next_tree,
          but rather to *only* the edges_in/edges_out
        Edges in can have one of three things happen to them:
        1. they get added to the next tree, as usual;
        2. they get postponed to the next tree,
            and are thus part of edges_in again next time;
        3. they get postponed but run out of span so they dissapear entirely.
        Edges out are similarly of four varieties:
        0. they are also in case (3) of edges_in, i.e., their extent was modified
            when they were in edges_in so that they now have extent 0;
        1. they get removed from the last tree, as usual;
        2. they get extended to the next tree,
            and are thus part of edges_out again next time;
        3. they are in fact a newly added edge, and so are part of edges_out next time.
        """
        self.ts = ts
        self.edges = ts.tables.edges.copy()
        self.new_left = ts.edges_left.copy()
        self.new_right = ts.edges_right.copy()
        self.forwards = forwards
        self.last_degree = np.full(ts.num_nodes, 0, dtype="int")
        self.next_degree = np.full(ts.num_nodes, 0, dtype="int")
        self.parent_out = np.full(ts.num_nodes, -1, dtype="int")
        self.parent_in = np.full(ts.num_nodes, -1, dtype="int")
        self.not_sample = [not n.is_sample() for n in ts.nodes()]
        self.next_nodes_edge = np.full(ts.num_nodes, -1, dtype="int")
        self.last_nodes_edge = np.full(ts.num_nodes, -1, dtype="int")

        if self.forwards:
            self.direction = 1
            # in C we can just modify these in place, but in
            # python they are (silently) immutable
            self.near_side = list(self.new_left)
            self.far_side = list(self.new_right)
        else:
            self.direction = -1
            self.near_side = list(self.new_right)
            self.far_side = list(self.new_left)

        self.edges_out = []
        self.edges_in = []

    def print_state(self):
        print("~~~~~~~~~~~~~~~~~~~~~~~~")
        print("edges in:", self.edges_in)
        print("parent in:", self.parent_in)
        print("edges out:", self.edges_out)
        print("parent out:", self.parent_out)
        print("last nodes edge:", self.last_nodes_edge)
        for e, _ in self.edges_out:
            print(
                "edge out:   ",
                "e =",
                e,
                "c =",
                self.edges.child[e],
                "p =",
                self.edges.parent[e],
                self.near_side[e],
                self.far_side[e],
            )

    def next_tree(self, tree_pos):
        # Clear out non-extended or postponed edges:
        # Note: maintaining parent_out is a bit tricky, because
        # if an edge from p->c has been extended, entirely replacing
        # another edge from p'->c, then both edges may be in edges_out,
        # and we only want to include the *first* one.
        direction = 1 if self.forwards else -1
        tmp = []
        for e, x in self.edges_out:
            self.parent_out[self.edges.child[e]] = tskit.NULL
            if x > 0:
                tmp.append([e, 0])
                assert self.near_side[e] != self.far_side[e]
                if x > 1:
                    # this is needed to catch newly-created edges
                    self.last_nodes_edge[self.edges.child[e]] = e
                    self.last_degree[self.edges.child[e]] += 1
                    self.last_degree[self.edges.parent[e]] += 1
            elif self.near_side[e] != self.far_side[e]:
                self.last_nodes_edge[self.edges.child[e]] = tskit.NULL
                self.last_degree[self.edges.child[e]] -= 1
                self.last_degree[self.edges.parent[e]] -= 1
        self.edges_out = tmp
        tmp = []
        for e, x in self.edges_in:
            self.parent_in[self.edges.child[e]] = tskit.NULL
            if x > 0:
                tmp.append([e, 0])
            elif self.near_side[e] != self.far_side[e]:
                assert self.last_nodes_edge[self.edges.child[e]] == tskit.NULL
                self.last_nodes_edge[self.edges.child[e]] = e
                self.last_degree[self.edges.child[e]] += 1
                self.last_degree[self.edges.parent[e]] += 1
        self.edges_in = tmp

        # done cleanup from last tree transition;
        # now we set the state up for this tree transition
        for j in range(tree_pos.out_range.start, tree_pos.out_range.stop, direction):
            e = tree_pos.out_range.order[j]
            if (self.parent_out[self.edges.child[e]] == tskit.NULL) and (
                self.near_side[e] != self.far_side[e]
            ):
                self.edges_out.append([e, False])

        for e, _ in self.edges_out:
            self.parent_out[self.edges.child[e]] = self.edges.parent[e]
            self.next_nodes_edge[self.edges.child[e]] = tskit.NULL
            self.next_degree[self.edges.child[e]] -= 1
            self.next_degree[self.edges.parent[e]] -= 1

        for j in range(tree_pos.in_range.start, tree_pos.in_range.stop, direction):
            e = tree_pos.in_range.order[j]
            self.edges_in.append([e, False])

        for e, _ in self.edges_in:
            self.parent_in[self.edges.child[e]] = self.edges.parent[e]
            assert self.next_nodes_edge[self.edges.child[e]] == tskit.NULL
            self.next_nodes_edge[self.edges.child[e]] = e
            self.next_degree[self.edges.child[e]] += 1
            self.next_degree[self.edges.parent[e]] += 1

    def check_state_at(self, pos, before, degree, nodes_edge):
        # if before=True then we construct the state at epsilon-on-near-side-of `pos`,
        # otherwise, at epsilon-on-far-side-of `pos`.
        check_degree = np.zeros(self.ts.num_nodes, dtype="int")
        check_nodes_edge = np.full(self.ts.num_nodes, -1, dtype="int")
        assert len(self.near_side) == self.edges.num_rows
        assert len(self.far_side) == self.edges.num_rows
        for j, (e, l, r) in enumerate(zip(self.edges, self.near_side, self.far_side)):
            overlaps = (l != r) and (
                ((pos - l) * (r - pos) > 0)
                or (r == pos and before)
                or (l == pos and not before)
            )
            if overlaps:
                check_degree[e.child] += 1
                check_degree[e.parent] += 1
                assert check_nodes_edge[e.child] == tskit.NULL
                check_nodes_edge[e.child] = j
        np.testing.assert_equal(check_nodes_edge, nodes_edge)
        np.testing.assert_equal(check_degree, degree)

    def check_parent(self, parent, edge_ids):
        temp_parent = np.full(self.ts.num_nodes, -1, dtype="int")
        for j in edge_ids:
            c = self.edges.child[j]
            p = self.edges.parent[j]
            temp_parent[c] = p
        np.testing.assert_equal(temp_parent, parent)

    def check_state(self, here):
        # error checking
        for e, x in self.edges_in:
            assert x == 0
            assert self.near_side[e] != self.far_side[e]
        for e, x in self.edges_out:
            assert x == 0
            assert self.near_side[e] != self.far_side[e]
        self.check_state_at(here, False, self.next_degree, self.next_nodes_edge)
        self.check_state_at(here, True, self.last_degree, self.last_nodes_edge)
        self.check_parent(self.parent_in, [j for j, x in self.edges_in if x == 0])
        self.check_parent(self.parent_out, [j for j, x in self.edges_out if x == 0])

    def add_or_extend_edge(self, child, new_parent, left, right):
        # print(f"add or extend: {child} -> {new_parent}")
        there = right if self.forwards else left
        old_edge = self.next_nodes_edge[child]
        if old_edge != tskit.NULL:
            old_parent = self.edges.parent[old_edge]
        else:
            old_parent = tskit.NULL
        if new_parent != old_parent:
            # if our new edge is in edges_out, it should be extended
            if self.parent_out[child] == new_parent:
                # print("extend edge: ", child, new_parent)
                e_out = self.last_nodes_edge[child]
                assert self.edges.child[e_out] == child
                assert self.edges.parent[e_out] == new_parent
                self.far_side[e_out] = there
                assert self.near_side[e_out] != self.far_side[e_out]
                for ex_out in self.edges_out:
                    if ex_out[0] == e_out:
                        break
                assert ex_out[0] == e_out
                ex_out[1] = 1
            else:
                # print("   new edge: ", child, new_parent)
                e_out = self.add_edge(new_parent, child, left, right)
                self.edges_out.append([e_out, 2])
            # If we're replacing the edge above this node, it must be in edges_in;
            # note that this assertion excludes the case that we're interrupting
            # an existing edge.
            assert (self.next_nodes_edge[child] == tskit.NULL) or (
                self.next_nodes_edge[child] in [e for e, _ in self.edges_in]
            )
            self.next_nodes_edge[child] = e_out
            self.next_degree[child] += 1
            self.next_degree[new_parent] += 1
            self.parent_out[child] = tskit.NULL
            if old_edge != tskit.NULL:
                for ex_in in self.edges_in:
                    if ex_in[0] == old_edge and (ex_in[1] == 0):
                        self.near_side[ex_in[0]] = there
                        if self.far_side[ex_in[0]] != there:
                            ex_in[1] = 1
                        self.next_nodes_edge[child] = tskit.NULL
                        self.next_degree[child] -= 1
                        self.next_degree[self.parent_in[child]] -= 1
                        self.parent_in[child] = tskit.NULL

    def add_edge(self, parent, child, left, right):
        new_id = self.edges.add_row(parent=parent, child=child, left=left, right=right)
        # note this appending should not be necessary in C
        if self.forwards:
            self.near_side.append(left)
            self.far_side.append(right)
        if not self.forwards:
            self.near_side.append(right)
            self.far_side.append(left)
        return new_id

    def mergeable(self, c):
        # returns True if the paths in parent_in and parent_out
        # up through nodes that aren't in the other tree
        # end at the same place and don't have conflicting times
        p_out = self.parent_out[c]
        p_in = self.parent_in[c]
        t_in = self.ts.nodes_time[p_in]
        t_out = self.ts.nodes_time[p_out]
        while (
            p_out != tskit.NULL
            and self.next_degree[p_out] == 0
            and self.not_sample[p_out]
        ) or (
            p_in != tskit.NULL and self.last_degree[p_in] == 0 and self.not_sample[p_in]
        ):
            if t_in == t_out:
                break
            elif t_in < t_out:
                p_in = self.parent_in[p_in]
                t_in = self.ts.nodes_time[p_in]
            else:
                p_out = self.parent_out[p_out]
                t_out = self.ts.nodes_time[p_out]
        return p_in == p_out and p_in != tskit.NULL

    def merge_paths(self, c, left, right):
        p_out = self.parent_out[c]
        p_in = self.parent_in[c]
        t_in = self.ts.nodes_time[p_in]
        t_out = self.ts.nodes_time[p_out]
        child = c
        while (t_in != t_out) and (
            (
                p_out != tskit.NULL
                and self.next_degree[p_out] == 0
                and self.not_sample[p_out]
            )
            or (
                p_in != tskit.NULL
                and self.last_degree[p_in] == 0
                and self.not_sample[p_in]
            )
        ):
            if t_in < t_out:
                self.add_or_extend_edge(child, p_in, left, right)
                child = p_in
                p_in = self.parent_in[p_in]
                t_in = self.ts.nodes_time[p_in]
            else:
                self.add_or_extend_edge(child, p_out, left, right)
                child = p_out
                p_out = self.parent_out[p_out]
                t_out = self.ts.nodes_time[p_out]
        assert p_out == p_in
        self.add_or_extend_edge(child, p_out, left, right)

    def extend_paths(self):
        tree_pos = tsutil.TreePosition(self.ts)
        if self.forwards:
            valid = tree_pos.next()
        else:
            valid = tree_pos.prev()
        while valid:
            left, right = tree_pos.interval
            # print(
            #     f'--------{"forwards" if self.forwards else "reverse"}, '
            #     f"{left}, {right}----------"
            # )
            # there = right if self.forwards else left
            here = left if self.forwards else right
            self.next_tree(tree_pos)
            self.check_state(here)
            for e_in, _ in self.edges_in:
                c = self.edges[e_in].child
                assert self.next_degree[c] > 0
                if self.last_degree[c] > 0:
                    if self.mergeable(c):
                        self.merge_paths(c, left, right)
            # end of loop, next tree
            if self.forwards:
                valid = tree_pos.next()
            else:
                valid = tree_pos.prev()
        if self.forwards:
            self.new_left = np.array(self.near_side)
            self.new_right = np.array(self.far_side)
        else:
            self.new_right = np.array(self.near_side)
            self.new_left = np.array(self.far_side)
        keep = np.full(self.edges.num_rows, True, dtype=bool)
        for j in range(self.edges.num_rows):
            left = self.new_left[j]
            right = self.new_right[j]
            if left < right:
                self.edges[j] = self.edges[j].replace(left=left, right=right)
            else:
                keep[j] = False
        self.edges.keep_rows(keep)


def extend_paths(ts, max_iter=10):
    tables = ts.dump_tables()
    mutations = tables.mutations.copy()
    tables.mutations.clear()

    last_num_edges = ts.num_edges
    for _ in range(max_iter):
        for forwards in [True, False]:
            # for t in ts.trees():
            #     print("------", t.interval)
            #     print(t.draw_text())
            extender = PathExtender(ts, forwards=forwards)
            extender.extend_paths()
            tables.edges.replace_with(extender.edges)
            tables.sort()
            tables.build_index()
            # print(";;;;", forwards, _, "now:", tables.edges.num_rows)
            # print(tables.edges)
            ts = tables.tree_sequence()
        if ts.num_edges == last_num_edges:
            break
        else:
            last_num_edges = ts.num_edges

    tables = ts.dump_tables()
    mutations = _slide_mutation_nodes_up(ts, mutations)
    tables.mutations.replace_with(mutations)
    tables.edges.squash()
    tables.sort()
    ts = tables.tree_sequence()
    return ts


def _path_pairs(tree):
    for c in tree.postorder():
        p = tree.parent(c)
        while p != tskit.NULL:
            yield (c, p)
            p = tree.parent(p)


def _path_up(c, p, tree, include_parent=False):
    # path from c up to p in tree, not including c or p
    c = tree.parent(c)
    while c != p and c != tskit.NULL:
        yield c
        c = tree.parent(c)
    assert c == p
    if include_parent:
        yield p


def _path_up_pairs(c, p, tree, others):
    # others should be a list of nodes
    otherdict = {tree.time(n): n for n in others}
    ot = min(otherdict)
    for n in _path_up(c, p, tree, include_parent=True):
        nt = tree.time(n)
        while ot < nt:
            on = otherdict.pop(ot)
            yield c, on
            c = on
            if len(otherdict) > 0:
                ot = min(otherdict)
            else:
                ot = np.inf
        yield c, n
        c = n
    assert n == p
    assert len(otherdict) == 0


def _path_overlaps(c, p, tree1, tree2):
    for n in _path_up(c, p, tree1):
        if n in tree2.nodes():
            return True
    return False


def _paths_mergeable(c, p, tree1, tree2):
    # checks that the nodes between c and p in each tree
    # are not present in the other tree
    # and their sets of times are disjoint
    nodes1 = set(tree1.nodes())
    nodes2 = set(tree2.nodes())
    assert c in nodes1, f"child node {c} not in tree1"
    assert p in nodes1, f"parent node {p} not in tree1"
    assert c in nodes2, f"child node {c} not in tree2"
    assert p in nodes2, f"parent node {p} not in tree2"
    path1 = set(_path_up(c, p, tree1))
    path2 = set(_path_up(c, p, tree2))
    times1 = {tree1.time(n) for n in path1}
    times2 = {tree2.time(n) for n in path2}
    return (
        (not _path_overlaps(c, p, tree1, tree2))
        and (not _path_overlaps(c, p, tree2, tree1))
        and len(times1.intersection(times2)) == 0
    )


def assert_not_extendable(ts):
    right_tree = ts.first()
    for tree in ts.trees():
        if tree.index + 1 >= ts.num_trees:
            break
        right_tree.seek_index(tree.index + 1)
        for c, p in _path_pairs(tree):
            extendable = (
                p != tree.parent(c)
                and c in right_tree.nodes(p)
                and p in right_tree.nodes()
                and _paths_mergeable(c, p, tree, right_tree)
            )
            if extendable:
                print("------------>", c, p)
                print(tree.draw_text())
                print(right_tree.draw_text())
            assert not extendable


def _extend_nodes(ts, interval, extendable):
    tables = ts.dump_tables()
    tables.edges.clear()
    mutations = tables.mutations.copy()
    tables.mutations.clear()
    left, right = interval
    # print("=================")
    # print("extending", left, right)
    extend_above = {}  # gives the new child->parent mapping
    todo_edges = np.repeat(True, ts.num_edges)
    tree = ts.at(left)
    for c, p, others in extendable:
        print("c:", c, "p:", p, "others:", others)
        others_not_done_yet = set(others) - set(extend_above)
        if len(others_not_done_yet) > 0:
            for cn, pn in _path_up_pairs(c, p, tree, others_not_done_yet):
                if cn not in extend_above:
                    assert cn not in extend_above
                    extend_above[cn] = pn
    for c, p in extend_above.items():
        e = tree.edge(c)
        if e == tskit.NULL or ts.edge(e).parent != p:
            # print("adding", c, p)
            tables.edges.add_row(child=c, parent=p, left=left, right=right)
            if e != tskit.NULL:
                edge = ts.edge(e)
                # adjust endpoints on existing edge
                for el, er in [
                    (max(edge.left, right), edge.right),
                    (edge.left, min(edge.right, left)),
                ]:
                    if el < er:
                        # print("replacing", edge, el, er)
                        tables.edges.append(edge.replace(left=el, right=er))
                todo_edges[e] = False
    for todo, edge in zip(todo_edges, ts.edges()):
        if todo:
            # print("retaining", edge)
            tables.edges.append(edge)
    tables.sort()
    ts = tables.tree_sequence()
    mutations = _slide_mutation_nodes_up(ts, mutations)
    tables.mutations.replace_with(mutations)
    tables.sort()
    return tables.tree_sequence()


def _naive_pass(ts, direction):
    assert direction in (-1, +1)
    num_trees = ts.num_trees
    if direction == +1:
        indexes = range(0, num_trees - 1, 1)
    else:
        indexes = range(num_trees - 1, 0, -1)
    for tj in indexes:
        extendable = []
        this_tree = ts.at_index(tj)
        next_tree = ts.at_index(tj + direction)
        # print("-----------", this_tree.index)
        # print(this_tree.draw_text())
        # print(next_tree.draw_text())
        for c, p in _path_pairs(this_tree):
            if (
                p != this_tree.parent(c)
                and p in next_tree.nodes()
                and c in next_tree.nodes(p)
            ):
                # print(c, p, " and ", list(next_tree.nodes(p)))
                if _paths_mergeable(c, p, this_tree, next_tree):
                    extendable.append((c, p, list(_path_up(c, p, this_tree))))
        # print("extending to", extendable)
        ts = _extend_nodes(ts, next_tree.interval, extendable)
        assert num_trees == ts.num_trees
    return ts


def naive_extend_paths(ts, max_iter=20):
    for _ in range(max_iter):
        ets = _naive_pass(ts, +1)
        ets = _naive_pass(ets, -1)
        if ets == ts:
            break
        ts = ets
    return ts


class TestExtendThings:
    """
    Common utilities in the two classes below.
    """

    def verify_simplify_equality(self, ts, ets):
        assert np.all(ts.genotype_matrix() == ets.genotype_matrix())
        assert ts.num_nodes == ets.num_nodes
        assert ts.num_samples == ets.num_samples
        t = ts.simplify().tables
        et = ets.simplify().tables
        et.assert_equals(t, ignore_provenance=True)

    def naive_verify(self, ts):
        ets = naive_extend_paths(ts)
        for i, t, et in ts.coiterate(ets):
            print("---------------", i)
            print(t.draw_text())
            print(et.draw_text())
        self.verify_simplify_equality(ts, ets)


class TestExtendPaths(TestExtendThings):
    """
    Test the 'extend_paths' method.
    """

    def get_example1(self):
        # 15.00|         |   13    |         |
        #      |         |    |    |         |
        # 12.00|   10    |   10    |    10   |
        #      |  +-+-+  |  +-+-+  |   +-+-+ |
        # 10.00|  8   |  |  |   |  |   8   | |
        #      |  |   |  |  |   |  |  ++-+ | |
        # 8.00 |  |   |  | 11  12  |  |  | | |
        #      |  |   |  |  |   |  |  |  | | |
        # 6.00 |  |   |  |  7   |  |  |  | | |
        #      |  |   |  |  |   |  |  |  | | |
        # 4.00 |  6   9  |  |   |  |  |  | | |
        #      |  |   |  |  |   |  |  |  | | |
        # 1.00 |  4   5  |  4   5  |  4  | 5 |
        #      | +++ +++ | +++ +++ | +++ | | |
        # 0.00 | 0 1 2 3 | 0 1 2 3 | 0 1 2 3 |
        #      0         3         6         9
        node_times = {
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 1,
            5: 1,
            6: 4,
            7: 6,
            8: 10,
            9: 4,
            10: 12,
            11: 8,
            12: 8,
            13: 15,
        }
        # (p,c,l,r)
        edges = [
            (4, 0, 0, 9),
            (4, 1, 0, 9),
            (5, 2, 0, 6),
            (5, 3, 0, 9),
            (6, 4, 0, 3),
            (9, 5, 0, 3),
            (7, 4, 3, 6),
            (11, 7, 3, 6),
            (12, 5, 3, 6),
            (8, 2, 6, 9),
            (8, 4, 6, 9),
            (8, 6, 0, 3),
            (10, 5, 6, 9),
            (10, 8, 0, 3),
            (10, 8, 6, 9),
            (10, 9, 0, 3),
            (10, 11, 3, 6),
            (10, 12, 3, 6),
            (13, 10, 3, 6),
        ]
        extended_path_edges = [
            (4, 0, 0.0, 9.0),
            (4, 1, 0.0, 9.0),
            (5, 2, 0.0, 6.0),
            (5, 3, 0.0, 9.0),
            (6, 4, 0.0, 9.0),
            (9, 5, 0.0, 9.0),
            (7, 6, 0.0, 9.0),
            (11, 7, 0.0, 9.0),
            (12, 9, 0.0, 9.0),
            (8, 2, 6.0, 9.0),
            (8, 11, 0.0, 9.0),
            (10, 8, 0.0, 9.0),
            (10, 12, 0.0, 9.0),
            (13, 10, 3.0, 6.0),
        ]
        samples = list(np.arange(4))
        tables = tskit.TableCollection(sequence_length=9)
        for (
            n,
            t,
        ) in node_times.items():
            flags = tskit.NODE_IS_SAMPLE if n in samples else 0
            tables.nodes.add_row(time=t, flags=flags)
        for p, c, l, r in edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ts = tables.tree_sequence()
        tables.edges.clear()
        for p, c, l, r in extended_path_edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ets = tables.tree_sequence()
        assert ts.num_edges == 19
        assert ets.num_edges == 14
        return ts, ets

    def get_example2(self):
        # 12.00|                     |          21         |                     |
        #      |                     |      +----+-----+   |                     |
        # 11.00|            20       |      |          |   |            20       |
        #      |        +----+---+   |      |          |   |        +----+---+   |
        # 10.00|        |       19   |      |         19   |        |       19   |
        #      |        |       ++-+ |      |        +-+-+ |        |       ++-+ |
        # 9.00 |       18       |  | |     18        |   | |       18       |  | |
        #      |     +--+--+    |  | |   +--+--+     |   | |     +--+--+    |  | |
        # 8.00 |     |     |    |  | |   |     |     |   | |    17     |    |  | |
        #      |     |     |    |  | |   |     |     |   | |   +-+-+   |    |  | |
        # 7.00 |     |     |   16  | |   |     |    16   | |   |   |   |    |  | |
        #      |     |     |   +++ | |   |     |   +-++  | |   |   |   |    |  | |
        # 6.00 |    15     |   | | | |   |     |   |  |  | |   |   |   |    |  | |
        #      |   +-+-+   |   | | | |   |     |   |  |  | |   |   |   |    |  | |
        # 5.00 |   |   |  14   | | | |   |    14   |  |  | |   |   |  14    |  | |
        #      |   |   |  ++-+ | | | |   |    ++-+ |  |  | |   |   |  ++-+  |  | |
        # 4.00 |  13   |  |  | | | | |  13    |  | |  |  | |  13   |  |  |  |  | |
        #      |  ++-+ |  |  | | | | |  ++-+  |  | |  |  | |  ++-+ |  |  |  |  | |
        # 3.00 |  |  | |  |  | | | | |  |  |  |  | | 12  | |  |  | |  |  | 12  | |
        #      |  |  | |  |  | | | | |  |  |  |  | | +++ | |  |  | |  |  | +++ | |
        # 2.00 | 11  | |  |  | | | | | 11  |  |  | | | | | | 11  | |  |  | | | | |
        #      | +++ | |  |  | | | | | +++ |  |  | | | | | | +++ | |  |  | | | | |
        # 1.00 | | | | | 10  | | | | | | | | 10  | | | | | | | | | | 10  | | | | |
        #      | | | | | +++ | | | | | | | | +++ | | | | | | | | | | +++ | | | | |
        # 0.00 | 0 7 4 9 2 5 6 1 3 8 | 0 7 4 2 5 6 1 3 9 8 | 0 7 4 1 2 5 6 3 9 8 |
        #      0                     3                     6                     9
        node_times = {
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 0,
            5: 0,
            6: 0,
            7: 0,
            8: 0,
            9: 0,
            10: 1,
            11: 2,
            12: 3,
            13: 4,
            14: 5,
            15: 6,
            16: 7,
            17: 8,
            18: 9,
            19: 10,
            20: 11,
            21: 12,
        }
        # (p,c,l,r)
        edges = [
            (10, 2, 0, 9),
            (10, 5, 0, 9),
            (11, 0, 0, 9),
            (11, 7, 0, 9),
            (12, 3, 3, 9),
            (12, 9, 3, 9),
            (13, 4, 0, 9),
            (13, 11, 0, 9),
            (14, 6, 0, 9),
            (14, 10, 0, 9),
            (15, 9, 0, 3),
            (15, 13, 0, 3),
            (16, 1, 0, 6),
            (16, 3, 0, 3),
            (16, 12, 3, 6),
            (17, 1, 6, 9),
            (17, 13, 6, 9),
            (18, 13, 3, 6),
            (18, 14, 0, 9),
            (18, 15, 0, 3),
            (18, 17, 6, 9),
            (19, 8, 0, 9),
            (19, 12, 6, 9),
            (19, 16, 0, 6),
            (20, 18, 0, 3),
            (20, 18, 6, 9),
            (20, 19, 0, 3),
            (20, 19, 6, 9),
            (21, 18, 3, 6),
            (21, 19, 3, 6),
        ]
        extended_path_edges = [
            (10, 2, 0.0, 9.0),
            (10, 5, 0.0, 9.0),
            (11, 0, 0.0, 9.0),
            (11, 7, 0.0, 9.0),
            (12, 3, 0.0, 9.0),
            (12, 9, 3.0, 9.0),
            (13, 4, 0.0, 9.0),
            (13, 11, 0.0, 9.0),
            (14, 6, 0.0, 9.0),
            (14, 10, 0.0, 9.0),
            (15, 9, 0.0, 3.0),
            (15, 13, 0.0, 9.0),
            (16, 1, 0.0, 6.0),
            (16, 12, 0.0, 9.0),
            (17, 1, 6.0, 9.0),
            (17, 15, 0.0, 9.0),
            (18, 14, 0.0, 9.0),
            (18, 17, 0.0, 9.0),
            (19, 8, 0.0, 9.0),
            (19, 16, 0.0, 9.0),
            (20, 18, 0.0, 3.0),
            (20, 18, 6.0, 9.0),
            (20, 19, 0.0, 3.0),
            (20, 19, 6.0, 9.0),
            (21, 18, 3.0, 6.0),
            (21, 19, 3.0, 6.0),
        ]
        samples = list(np.arange(10))
        tables = tskit.TableCollection(sequence_length=9)
        for (
            n,
            t,
        ) in node_times.items():
            flags = tskit.NODE_IS_SAMPLE if n in samples else 0
            tables.nodes.add_row(time=t, flags=flags)
        for p, c, l, r in edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ts = tables.tree_sequence()
        tables.edges.clear()
        for p, c, l, r in extended_path_edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ets = tables.tree_sequence()
        assert ts.num_edges == 30
        assert ets.num_edges == 26
        return ts, ets

    def get_example3(self):
        # Here is the full tree; extend edges should be able to
        # recover all unary nodes after simplification:
        #
        #       9         9         9          9
        #     +-+-+    +--+--+  +---+---+  +-+-+--+
        #     8   |    8     |  8   |   |  8 | |  |
        #     |   |  +-+-+   |  |   |   |  | | |  |
        #     7   |  |   7   |  |   7   |  | | |  7
        #   +-+-+ |  | +-++  |  | +-++  |  | | |  |
        #   6   | |  | |  6  |  | |  6  |  | | |  6
        # +-++  | |  | |  |  |  | |  |  |  | | |  |
        # 1  0  2 3  1 2  0  3  1 2  0  3  1 2 3  0
        #   +++          +++        +++          +++
        #   4 5          4 5        4 5          4 5
        #
        samples = [0, 1, 2, 3, 4, 5]
        node_times = [1, 1, 1, 1, 0, 0, 2, 3, 4, 5]
        # (p, c, l, r)
        edges = [
            (0, 4, 0, 10),
            (0, 5, 0, 10),
            (6, 0, 0, 10),
            (6, 1, 0, 3),
            (7, 2, 0, 7),
            (7, 6, 0, 10),
            (8, 1, 3, 10),
            (8, 7, 0, 5),
            (9, 2, 7, 10),
            (9, 3, 0, 10),
            (9, 7, 5, 10),
            (9, 8, 0, 10),
        ]
        tables = tskit.TableCollection(sequence_length=10)
        for n, t in enumerate(node_times):
            flags = tskit.NODE_IS_SAMPLE if n in samples else 0
            tables.nodes.add_row(time=t, flags=flags)
        for p, c, l, r in edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ts = tables.tree_sequence()
        return ts

    def verify_extend_paths(self, ts, max_iter=10):
        ets = extend_paths(ts, max_iter=max_iter)
        self.verify_simplify_equality(ts, ets)

    def test_runs(self):
        ts = msprime.simulate(5, mutation_rate=1.0, random_seed=126)
        self.verify_extend_paths(ts)
        self.naive_verify(ts)

    @pytest.mark.skip("TODO")
    def test_migrations_disallowed(self):
        ts = msprime.simulate(5, mutation_rate=1.0, random_seed=126)
        tables = ts.dump_tables()
        tables.populations.add_row()
        tables.populations.add_row()
        tables.migrations.add_row(0, 1, 0, 0, 1, 0)
        ts = tables.tree_sequence()
        with pytest.raises(
            _tskit.LibraryError, match="TSK_ERR_MIGRATIONS_NOT_SUPPORTED"
        ):
            _ = ts.extend_paths()

    @pytest.mark.skip("TODO")
    def test_unknown_times(self):
        ts = msprime.simulate(5, mutation_rate=1.0, random_seed=126)
        tables = ts.dump_tables()
        tables.mutations.clear()
        for mut in ts.mutations():
            tables.mutations.append(mut.replace(time=tskit.UNKNOWN_TIME))
        ts = tables.tree_sequence()
        with pytest.raises(
            _tskit.LibraryError, match="TSK_ERR_DISALLOWED_UNKNOWN_MUTATION_TIME"
        ):
            _ = ts.extend_paths()

    @pytest.mark.skip("TODO")
    def test_max_iter(self):
        ts = msprime.simulate(5, random_seed=126)
        with pytest.raises(_tskit.LibraryError, match="positive"):
            ets = ts.extend_paths(max_iter=0)
        with pytest.raises(_tskit.LibraryError, match="positive"):
            ets = ts.extend_paths(max_iter=-1)
        ets = ts.extend_paths(max_iter=1)
        et = ets.extend_paths(max_iter=1).dump_tables()
        eet = ets.extend_paths(max_iter=2).dump_tables()
        eet.assert_equals(et)

    def test_very_simple(self):
        samples = [0]
        node_times = [0, 1, 2, 3]
        # (p, c, l, r)
        edges = [
            (1, 0, 0, 1),
            (2, 0, 1, 2),
            (2, 1, 0, 1),
            (3, 0, 2, 3),
            (3, 2, 0, 2),
        ]
        correct_edges = [
            (1, 0, 0, 3),
            (2, 1, 0, 3),
            (3, 2, 0, 3),
        ]
        tables = tskit.TableCollection(sequence_length=3)
        for n, t in enumerate(node_times):
            flags = tskit.NODE_IS_SAMPLE if n in samples else 0
            tables.nodes.add_row(time=t, flags=flags)
        for p, c, l, r in edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ts = tables.tree_sequence()
        ets = extend_paths(ts)
        etables = ets.tables
        correct_tables = etables.copy()
        correct_tables.edges.clear()
        for p, c, l, r in correct_edges:
            correct_tables.edges.add_row(parent=p, child=c, left=l, right=r)
        print(etables.edges)
        print(correct_tables.edges)
        etables.assert_equals(correct_tables, ignore_provenance=True)
        self.naive_verify(ts)

    def test_example1(self):
        ts, ets = self.get_example1()
        test_ets = extend_paths(ts)
        test_ets.tables.assert_equals(ets.tables, ignore_provenance=True)
        self.verify_extend_paths(ts)
        self.naive_verify(ts)

    def test_example2(self):
        ts, ets = self.get_example2()
        test_ets = extend_paths(ts)
        test_ets.tables.assert_equals(ets.tables, ignore_provenance=True)
        self.verify_extend_paths(ts)
        self.naive_verify(ts)

    def test_example3(self):
        ts = self.get_example3()
        sts = ts.simplify()
        assert ts.num_edges == 12
        assert sts.num_edges == 16
        ts.tables.assert_equals(extend_paths(sts).tables, ignore_provenance=True)
        self.naive_verify(ts)

    def test_internal_samples(self):
        # Now we should have the same but not extend 5 (where * is):
        #
        #    6         6      6         6
        #  +-+-+     +-+-+  +-+-+     +-+-+
        #  7   *     7   *  7   8     7   8
        #  |   |    ++-+ |  | +-++    |   |
        #  4   5    4  | *  4 |  5    4   5
        # +++ +++  +++ | |  | | +++  +++ +++
        # 0 1 2 3  0 1 2 3  0 1 2 3  0 1 2 3
        #
        node_times = {
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 1.0,
            5: 1.0,
            6: 3.0,
            7: 2.0,
            8: 2.0,
        }
        # (p, c, l, r)
        edges = [
            (4, 0, 0, 10),
            (4, 1, 0, 5),
            (4, 1, 7, 10),
            (5, 2, 0, 2),
            (5, 2, 5, 10),
            (5, 3, 0, 2),
            (5, 3, 5, 10),
            (7, 2, 2, 5),
            (7, 4, 0, 10),
            (8, 1, 5, 7),
            (8, 5, 5, 10),
            (6, 3, 2, 5),
            (6, 5, 0, 2),
            (6, 7, 0, 10),
            (6, 8, 5, 10),
        ]
        tables = tskit.TableCollection(sequence_length=10)
        samples = [0, 1, 2, 3, 5]
        for n, t in node_times.items():
            flags = tskit.NODE_IS_SAMPLE if n in samples else 0
            tables.nodes.add_row(time=t, flags=flags)
        for p, c, l, r in edges:
            tables.edges.add_row(parent=p, child=c, left=l, right=r)
        ts = tables.tree_sequence()
        ets = extend_paths(ts)
        ets.tables.assert_equals(tables)
        self.verify_extend_paths(ts)
        self.naive_verify(ts)

    @pytest.mark.skip("TODO: this one apparently has a split edge (on windows anyhow).")
    def test_example_split_edge(self):
        ts = msprime.sim_ancestry(
            100,
            population_size=1000,
            sequence_length=1e6,
            coalescing_segments_only=False,
            random_seed=12,
            rcombination_rate=1e-8,
        )
        ets = extend_paths(ts)
        self.verify_simplify_equality(ts, ets)

    @pytest.mark.parametrize("seed", [3, 4, 5, 6])
    def test_wf(self, seed):
        tables = wf.wf_sim(N=6, ngens=9, num_loci=100, deep_history=False, seed=seed)
        tables.sort()
        ts = tables.tree_sequence().simplify()
        self.verify_extend_paths(ts)
        self.naive_verify(ts)


@pytest.mark.skip("TODO")
class TestExamples:
    """
    Compare the ts method with local implementation.
    """

    def check(self, ts):
        if np.any(tskit.is_unknown_time(ts.mutations_time)):
            tables = ts.dump_tables()
            tables.compute_mutation_times()
            ts = tables.tree_sequence()
        py_ts = extend_paths(ts)
        lib_ts = ts.extend_paths()
        lib_ts.tables.assert_equals(py_ts.tables)
        assert np.all(ts.genotype_matrix() == lib_ts.genotype_matrix())
        sts = ts.simplify()
        lib_sts = lib_ts.simplify()
        lib_sts.tables.assert_equals(sts.tables, ignore_provenance=True)

    # @pytest.mark.parametrize("ts", get_example_tree_sequences())
    def test_suite_examples_defaults(self, ts):
        if ts.num_migrations == 0:
            self.check(ts)
        else:
            with pytest.raises(
                _tskit.LibraryError, match="TSK_ERR_MIGRATIONS_NOT_SUPPORTED"
            ):
                _ = ts.extend_paths()

    @pytest.mark.parametrize("n", [3, 4, 5])
    def test_all_trees_ts(self, n):
        ts = tsutil.all_trees_ts(n)
        self.check(ts)
