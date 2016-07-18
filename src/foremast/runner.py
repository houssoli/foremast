#!/usr/bin/env python
"""A runner for all of the spinnaker pipe modules.

Read environment variables from Jenkins:

    * EMAIL
    * ENV
    * GIT_REPO
    * PROJECT
    * REGION

Then run specific prepare jobs.
"""
import logging
import os

import gogoutils

from foremast import (app, configs, consts, dns, elb, iam, pipeline, s3,
                      securitygroup, slacknotify, utils, autoscaling_policy)

LOG = logging.getLogger(__name__)
logging.basicConfig(format=consts.LOGGING_FORMAT)
logging.getLogger("foremast").setLevel(logging.INFO)


class ForemastRunner(object):
    """Wrap each pipes module in a way that is easy to invoke."""

    def __init__(self):
        """Setup the Runner for all Foremast modules."""
        self.email = os.getenv("EMAIL")
        self.env = os.getenv("ENV")
        self.group = os.getenv("PROJECT")
        self.region = os.getenv("REGION")
        self.repo = os.getenv("GIT_REPO")

        self.git_project = "{}/{}".format(self.group, self.repo)
        parsed = gogoutils.Parser(self.git_project)
        generated = gogoutils.Generator(*parsed.parse_url())

        self.app = generated.app
        self.trigger_job = generated.jenkins()['name']
        self.git_short = generated.gitlab()['main']

        self.raw_path = "./raw.properties"
        self.json_path = self.raw_path + ".json"
        self.configs = None

    def write_configs(self):
        """Generate the configurations needed for pipes."""
        utils.banner("Generating Configs")
        app_configs = configs.process_git_configs(
            git_short=self.git_short)
        self.configs = configs.write_variables(app_configs=app_configs,
                                               out_file=self.raw_path,
                                               git_short=self.git_short)

    def create_app(self):
        """Create the spinnaker application."""
        utils.banner("Creating Spinnaker App")
        spinnakerapp = app.SpinnakerApp(app=self.app,
                                        email=self.email,
                                        project=self.group,
                                        repo=self.repo)
        spinnakerapp.create_app()

    def create_pipeline(self, onetime=None):
        """Create the spinnaker pipeline(s)."""
        utils.banner("Creating Pipeline")

        kwargs = {
            'app': self.app,
            'trigger_job': self.trigger_job,
            'prop_path': self.json_path,
            'base': None,
        }

        if not onetime:
            spinnakerpipeline = pipeline.SpinnakerPipeline(**kwargs)
        else:
            spinnakerpipeline = pipeline.SpinnakerPipelineOnetime(
                onetime=onetime, **kwargs)

        spinnakerpipeline.create_pipeline()

    def create_iam(self):
        """Create IAM resources."""
        utils.banner("Creating IAM")
        iam.create_iam_resources(env=self.env, app=self.app)

    def create_s3(self):
        """Create S3 bucket for Archaius."""
        utils.banner("Creating S3")
        s3.init_properties(env=self.env, app=self.app)

    def create_secgroups(self):
        """Create security groups as defined in the configs."""
        utils.banner("Creating Security Group")
        sgobj = securitygroup.SpinnakerSecurityGroup(app=self.app,
                                                     env=self.env,
                                                     region=self.region,
                                                     prop_path=self.json_path)
        sgobj.create_security_group()

    def create_elb(self):
        """Create the ELB for the defined environment."""
        utils.banner("Creating ELB")
        elbobj = elb.SpinnakerELB(app=self.app,
                                  env=self.env,
                                  region=self.region,
                                  prop_path=self.json_path)
        elbobj.create_elb()

    def create_dns(self):
        """Create DNS for the defined app and environment."""
        utils.banner("Creating DNS")
        elb_subnet = self.configs[self.env]['elb']['subnet_purpose']
        dnsobj = dns.SpinnakerDns(app=self.app,
                                  env=self.env,
                                  region=self.region,
                                  prop_path=self.json_path,
                                  elb_subnet=elb_subnet)
        dnsobj.create_elb_dns()

    def create_autoscaling_policy(self):
        """Create Scaling Policy for app in environment"""
        utils.banner("Creating Scaling Policy")
        policyobj = autoscaling_policy.AutoScalingPolicy(
                                            app=self.app,
                                            env=self.env,
                                            region=self.region,
                                            prop_path=self.json_path)
        policyobj.create_policy()

    def slack_notify(self):
        """Send out a slack notification."""
        utils.banner("Sending slack notification")

        if self.env.startswith("prod"):
            notify = slacknotify.SlackNotification(app=self.app,
                                                   env=self.env,
                                                   prop_path=self.json_path)
            notify.post_message()
        else:
            LOG.info("No slack message sent, not production environment")

    def cleanup(self):
        """Clean up generated files."""
        os.remove(self.raw_path)


def prepare_infrastructure():
    """Entry point for preparing the infrastructure in a specific env."""
    runner = ForemastRunner()

    runner.write_configs()
    runner.create_iam()
    runner.create_s3()
    runner.create_secgroups()

    eureka = runner.configs[runner.env]['app']['eureka_enabled']

    if eureka:
        LOG.info("Eureka Enabled, skipping ELB and DNS setup")
    else:
        LOG.info("No Eureka, running ELB and DNS setup")
        runner.create_elb()
        runner.create_dns()
        LOG.info("Eureka Enabled, skipping ELB and DNS setup")

    runner.slack_notify()
    runner.cleanup()


def prepare_app_pipeline():
    """Entry point for application setup and initial pipeline in Spinnaker."""
    runner = ForemastRunner()
    runner.create_app()
    runner.write_configs()
    runner.create_pipeline()
    runner.cleanup()


def prepare_onetime_pipeline():
    """Entry point for single use pipeline setup in the defined app."""
    runner = ForemastRunner()
    runner.write_configs()
    runner.create_pipeline(onetime=os.getenv('ENV'))
    runner.cleanup()

def create_scaling_policy():
    runner = ForemastRunner()
    runner.write_configs()
    runner.create_autoscaling_policy()
    runner.cleanup()

def rebuild_pipelines():
    """ Entry point for rebuilding pipelines. Can be used to rebuild all pipelines
        or a specific group """
    all_apps = utils.get_all_apps()
    rebuild_project = os.getenv("REBUILD_PROJECT")
    if rebuild_project is None:
        msg = 'No REBUILD_PROJECT variable found'
        LOG.fatal(msg)
        raise SystemExit('Error: {0}'.format(msg))

    for app in all_apps:
        if not 'repoProjectKey' in app:
            LOG.info("Skipping {}. No project key found".format(app['name']))
            continue
        if (app['repoProjectKey'].lower() == rebuild_project.lower() or
                rebuild_project == 'ALL' ):
            os.environ["PROJECT"] = app['repoProjectKey']
            os.environ["GIT_REPO"] = app['repoSlug']
            LOG.info('Rebuilding pipelines for {}/{}'.format(
                    app['repoProjectKey'], app['repoSlug']))
            runner = ForemastRunner()
            runner.write_configs()
            runner.create_pipeline()
            runner.cleanup()
