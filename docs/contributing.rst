Contributing
************

This page walks you through how to contribute to the development of YSE_PZ.

We want your help. No, really. There may be a little voice inside your head that
is telling you that you're not ready to be an open source contributor; that your
skills aren't nearly good enough to contribute. What could you possibly offer a
project like this one?

We assure you - the little voice in your head is wrong. If you can write code or
documentation, you can contribute code to open source. Contributing to open
source projects is a fantastic way to advance one's coding and open source
workflow skills. Writing perfect code isn't the measure of a good developer
(that would disqualify all of us!); it's trying to create something, making
mistakes, and learning from those mistakes. That's how we all improve, and we
are happy to help others learn.

Being an open source contributor doesn't just mean writing code, either. You can
help out by writing documentation, tests, or even giving feedback about the
project (and yes - that includes giving feedback about the contribution
process). Some of these contributions may be the most valuable to the project
as a whole, because you're coming to the project with fresh eyes, so you can
see the errors and assumptions that seasoned contributors have glossed over.

.. note::
    This text was originally written by Adrienne Lowe for a PyCon talk,
    and was adapted by YSE_PZ based on its use in the README file for the MetPy
    project and `Astropy <https://www.astropy.org>`_ project.

General workflow
----------------

YSE_PZ has three long-lived branches: ``experimental`` (feature work, deployed to
the ``/yse_experimental/`` test stack), ``develop`` (deployed to ``/yse_test/``) and
``master`` (production). New work never targets ``develop`` or ``master`` directly.
The full step-by-step workflow, including issue linking and who merges what, is in
`CONTRIBUTING.md <https://github.com/Young-Supernova-Experiment/YSE_PZ/blob/experimental/CONTRIBUTING.md>`_;
in short:

1. Open a GitHub issue for each individual change.
2. Branch from ``experimental`` (one branch per group of related issues).
3. Open a pull request into ``experimental`` that links each issue with ``Fixes #N``.
4. Once CI passes it is merged and deployed to ``/yse_experimental/`` for testing.
5. Tested work is promoted ``experimental`` -> ``develop`` -> ``master`` by the maintainers.

Clone the repository and create your branch from ``experimental``:

.. code:: none

    git clone https://github.com/Young-Supernova-Experiment/YSE_PZ.git
    cd YSE_PZ
    git checkout experimental
    git checkout -b fix/<short-description>
    git push --set-upstream origin fix/<short-description>

Next go to the YSE_PZ github repository page and go to the pull requests tab.

.. image:: _static/contributing_pull_request_tab.png

Then open a new draft pull request.

.. image:: _static/contributing_new_pull_request.png

Create a pull request from your branch into ``experimental`` (set it as the base branch).

.. image:: _static/contributing_create_pull_request.png

Fill in the title and describe what you are trying to do in the description, and
open a draft pull request.

.. image:: _static/contributing_draft_pull_request.png

As you commit and push changes to your branch on github they will show up
in the draft pull request. When you are a happy for you changes to be reviewed
and then eventually merged into experimental, click ready for review.

.. image:: _static/contributing_ready_for_review.png

Once CI passes it will be merged into
experimental. After your branch has been merged, delete the branch from your local
repository.

.. code:: none

    git branch -d <your branch name>

GitHub deletes the merged branch automatically.


Documentation
-------------

Contributing to the documentation is probably the best place to start. Writing and
building the documentation locally is straightforward. For most documentation
contributions you'll be working inside of the `docs/` directory. The
documentation is build using `sphinx <https://www.sphinx-doc.org/en/master/>`_
and you should check our their documentation for the basics.

In order to build the docs and view them your browser locally you will need to
install a couple of packages. To do this we recommend using a
`conda <https://docs.conda.io/en/latest/>`_ environment. Once you have installed
conda, go head and create a new environment.

.. code:: none

    conda create --name yse_pz_docs

The activate the environment.

.. code:: none

    conda activate yse_pz_docs

Then `pip <https://pip.pypa.io/en/stable/cli/pip_install/>`_ install the
documentation requirements.

.. code:: none

    pip install -r docs/requirements.txt

Then go into the docs/ directory and if all is working you should build the
documentation with

.. code:: none

    make html

After you have run the build command open the `docs/_build/html/index.html`
file in your web browser and you should see the YSE_PZ documentation. As you
make changes and additions to the documentation you can build it locally and
check that nothing breaks.

When you push changes to a draft or open pull request, github will build a
preview of the documentation automatically for you. You can see this preview
here.

.. image:: _static/contributing_docs_ci.png


Updating dependencies
---------------------

If you need to add a new package to YSE_PZ or update an existing one you have to
check whether the dependency update will work with all of YSE_PZ's currently 
installed packages. The way you do this is to add your proposed dependency to the
:code:`docker/requirements.web.dev` file. For example if you wanted to have numpy
version 1.22.3. the contents of :code:`docker/requirements.web.dev` would be

.. code:: none

    numpy==1.22.3

To test if your proposed dependency will work, you need to spin up a dev docker
container which will attempt to install your proposed dependency and then 
run YSE_PZ. To do this run the command, whilst in the :code:`YSE_PZ/docker/`
directory,

.. code:: none

    docker-compose -f docker-compose.dev.yaml up --build

If this throws no errors, and YSE_PZ runs without issue, then the dependency update
was successful. Running this command creates a local image called,
:code:`local/yse_pz_web:dev`. If you want to run with a PyCharm debugger
attached, you can temporarily point the `image` property of the `web` service
to this new image name. At this point you can check your
:code:`docker/requirements.web.dev` into a pull request into develop.
Eventually when your code gets pull into master and deployed your dependency
will be absorbed into the latest YSE_PZ docker image.

ONLY CHECK-IN the :code:`requirements.web.dev`. DO NOT CHECK-IN changes to the
docker-compose files.



 
