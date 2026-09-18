"""Validate migrated code and run solver-backed smoke/regression tests.

Run: python tests/validate_notebooks.py --execute --compare-ref origin/main
Uses the installed AMPL runtime/license. It never activates a license or installs
packages. The default smoke fixtures fit the AMPL demo license and exercise every
migrated model and plotting path; --full keeps the original large instances.
"""
import argparse
import contextlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(cell):
    value = cell.get('source', [])
    return ''.join(value) if isinstance(value, list) else value


def clean(code):
    return '\n'.join(line for line in code.splitlines()
                     if not line.lstrip().startswith(('%', '!')))


def smoke(code, path, original=False):
    if path.stem == '3' and path.parent.name == 'Location':
        code = code.replace('n=60, p=8', 'n=10, p=4')
    elif path.stem == '10':
        # Two remote triangles force SEC separation (distance 1 within each).
        if '# Cities coordinates' in code:
            code += '\ncities = [(str(i),lat,lon) for i,(lat,lon) in enumerate([(0,0),(0,.01),(.01,0),(10,10),(10,10.01),(10.01,10)])]\nn = len(cities)\n'
    elif path.stem == '9':
        code = code.replace('range(44)', 'range(8)')
    elif path.stem == '11':
        code = code.replace('n = 18', 'n = 8').replace('vehicles = [1, 2, 3, 4, 5]', 'vehicles = [1, 2]')
    elif path.stem == '12':
        code = code.replace('num_customers=15, num_vehicles=3', 'num_customers=6, num_vehicles=2')
    elif path.stem == '14':
        code = code.replace('num_jobs = 10', 'num_jobs = 4').replace('num_machines = 6', 'num_machines = 3')
        if original and 'import random' in code:
            code = code.replace('import random', 'import random\nrandom.seed(2026)')
    return code


def execute(nb, path, full=False, original=False, solver='highs'):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    namespace = {'__name__': '__notebook_test__'}
    objectives = []
    for idx, cell in enumerate(nb['cells']):
        if cell['cell_type'] != 'code':
            continue
        code = clean(source(cell))
        if not code.strip():
            continue
        if path.parent.name == 'Intro' and idx >= 13:
            continue  # GILP is not part of the migrated solver API.
        if not original:
            code = code.replace('runtime = ampl_notebook(modules=[SOLVER], license_uuid=LICENSE_UUID)', 'runtime = AMPL()')
            code = code.replace('SOLVER = "highs"', f'SOLVER = "{solver}"')
        if not full:
            code = smoke(code, path, original)
        is_solve = ('solve_checked(' in code or '.optimize(' in code) and 'def solve_checked' not in code and not re.search(r'^def ', code, re.M)
        # Also record solves triggered by the location plotting functions.
        plot_solve = bool(re.search(r'^(?:model, distances, selected, pts = )?solve_and_plot_', code, re.M))
        is_solve |= plot_solve
        if plot_solve and path.stem in ('1','2'):
            code = re.sub(r'^solve_and_plot_', '_plot_result = solve_and_plot_', code, flags=re.M)
        is_solve |= 'model, x, y = solve_model(' in code
        with contextlib.redirect_stdout(io.StringIO()):
            previous_directory = os.getcwd()
            with tempfile.TemporaryDirectory(prefix='in2009b-validation-') as directory:
                try:
                    os.chdir(directory)  # Original notebooks export .lp files.
                    exec(compile(code, f'{path}:{idx}', 'exec'), namespace)
                finally:
                    os.chdir(previous_directory)
        if is_solve:
            if plot_solve:
                model = namespace.get('model') if path.stem == '3' else namespace.get('_plot_result')
            else:
                model = namespace.get('model') if path.stem in ('8','14') else namespace.get('m')
            if model is not None:
                value = model.ObjVal if original else model.obj['Total_Cost'].value()
                objectives.append(float(value))
                if not original:
                    for name, constraint in model.get_constraints():
                        for index, instance in constraint:
                            body = instance.body()
                            assert instance.lb()-1e-5 <= body <= instance.ub()+1e-5, (name, index, body)
        plt.close('all')
    # Explicit data-level feasibility checks for routing and job shop.
    if path.stem in ('11', '12'):
        routes = namespace['routes']
        visited = [i for route in routes.values() for i in route if i != 0]
        assert sorted(visited) == sorted(namespace['customers'])
        if path.stem == '12':
            assert all(sum(namespace['demand'][i] for i in route) <= namespace['Q']+1e-6 for route in routes.values())
    if path.stem == '10' and not original and not full:
        assert namespace['cut_count'] > 0, 'Smoke fixture must exercise SEC separation'
    if path.stem == '9':
        arcs = namespace['solution']
        successor = dict(arcs)
        assert len(successor) == len(namespace['cities'])
        visited, node = set(), 0
        while node not in visited:
            visited.add(node)
            node = successor[node]
        assert node == 0 and visited == set(namespace['cities']), 'Disconnected TSP tour'
    if path.stem == '14' and not original:
        start = namespace['start']
        jobs = namespace['jobs']
        for j, operations in jobs.items():
            for i, (machine, duration) in enumerate(operations):
                assert start[j,i,machine] >= -1e-6
                assert start[j,i,machine]+duration <= namespace['Cmax']+1e-6
                if i+1 < len(operations):
                    assert start[j,i+1,operations[i+1][0]] >= start[j,i,machine]+duration-1e-6
        entries = list(start)
        for idx, a in enumerate(entries):
            for b in entries[idx+1:]:
                if a[2] == b[2]:
                    assert (start[a]+jobs[a[0]][a[1]][1] <= start[b]+1e-6 or
                            start[b]+jobs[b[0]][b[1]][1] <= start[a]+1e-6)
    return objectives


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--compare-ref')
    parser.add_argument('--solver', choices=('highs','gurobi'), default='highs')
    args = parser.parse_args()
    migrated = [p for p in sorted(ROOT.rglob('*.ipynb')) if p.stem != '13']
    assert len(migrated) == 14
    for path in migrated:
        nb = json.loads(path.read_text())
        import nbformat
        nbformat.validate(nbformat.from_dict(nb))
        for idx, cell in enumerate(nb['cells']):
            if cell['cell_type'] == 'code':
                code = clean(source(cell))
                assert not re.search(r'gurobipy|\bGRB\.|\bgp\.|\.X\b|\.optimize\(', code), (path, idx)
                compile(code, f'{path}:{idx}', 'exec')
                assert cell.get('execution_count') is None and not cell.get('outputs')
        relative = path.relative_to(ROOT)
        if args.execute:
            results = execute(nb, path, args.full, solver=args.solver)
            if args.compare_ref:
                raw = subprocess.check_output(['git','show',f'{args.compare_ref}:{relative}'], cwd=ROOT)
                reference = execute(json.loads(raw), path, args.full, original=True)
                assert len(results) == len(reference), (relative, results, reference)
                assert all(math.isclose(a,b,rel_tol=1e-6,abs_tol=1e-5) for a,b in zip(results,reference)), (relative,results,reference)
            print(f'PASS {relative}: {results}')
        else:
            print(f'PASS {relative}: schema and code')
    if args.execute:
        nb = json.loads((ROOT/'notebooks/Routing-Scheduling/12.ipynb').read_text())
        # Unused vehicles must not create an infinite route-reconstruction loop.
        nb['cells'][2]['source'] = source(nb['cells'][2]).replace('vehicles = [1, 2]', 'vehicles = [1, 2, 3, 4, 5, 6, 7]')
        nb['cells'] = nb['cells'][:5]
        execute(nb, ROOT/'notebooks/Routing-Scheduling/12.ipynb', solver=args.solver)
        print('PASS CVRP unused-vehicle route extraction')
        setup = clean(source(nb['cells'][1])).replace('runtime = ampl_notebook(modules=[SOLVER], license_uuid=LICENSE_UUID)', 'runtime = AMPL()')
        setup = setup.replace('SOLVER = "highs"', f'SOLVER = "{args.solver}"')
        namespace = {}
        with contextlib.redirect_stdout(io.StringIO()):
            exec(setup, namespace)
            model = namespace['new_ampl']()
            model.eval('var x >= 0; minimize Total_Cost: x; subject to Impossible: x <= -1;')
            try:
                namespace['solve_checked'](model)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Infeasible models must block solution extraction')
        print('PASS infeasible-model extraction guard')
    print(f'Validated {len(migrated)} migrated notebooks. PyVRP notebook excluded and unchanged.')


if __name__ == '__main__':
    main()
