import os
import random
import re
import shutil
from typing import List, Text, Optional

from langchain import PromptTemplate
from langchain.schema import SystemMessage, HumanMessage, AIMessage
from pydantic.dataclasses import dataclass

from src.apis import gpt
from src.apis.jina_cloud import process_error_message, push_executor, is_executor_in_hub
from src.constants import FILE_AND_TAG_PAIRS, NUM_IMPLEMENTATION_STRATEGIES, MAX_DEBUGGING_ITERATIONS, \
    PROBLEMATIC_PACKAGES, EXECUTOR_FILE_NAME, EXECUTOR_FILE_TAG, TEST_EXECUTOR_FILE_NAME, TEST_EXECUTOR_FILE_TAG, \
    REQUIREMENTS_FILE_NAME, REQUIREMENTS_FILE_TAG, DOCKER_FILE_NAME, DOCKER_FILE_TAG, UNNECESSARY_PACKAGES
from src.options.generate.templates_system import template_system_message_base, gpt_example, executor_example, \
    docarray_example, client_example, system_task_iteration, system_task_introduction, system_test_iteration
from src.options.generate.templates_user import template_generate_microservice_name, \
    template_generate_possible_packages, \
    template_solve_code_issue, \
    template_solve_dependency_issue, template_is_dependency_issue, template_generate_playground, \
    template_generate_executor, template_generate_test, template_generate_requirements, template_generate_dockerfile, \
    template_chain_of_thought, template_summarize_error, template_generate_possible_packages_output_format_string
from src.options.generate.ui import get_random_employee
from src.utils.io import persist_file, get_all_microservice_files_with_content, get_microservice_path
from src.utils.string_tools import print_colored


@dataclass
class TaskSpecification:
    task: Optional[Text]
    test: Optional[Text]

class Generator:
    def __init__(self, task_description, test_description, model='gpt-4'):
        self.gpt_session = gpt.GPTSession(task_description, test_description, model=model)
        self.microservice_specification = TaskSpecification(task=task_description, test=test_description)

    def extract_content_from_result(self, plain_text, file_name, match_single_block=False):

        pattern = fr"^\*\*{file_name}\*\*\n```(?:\w+\n)?([\s\S]*?)\n```" # the \n at the end makes sure that ``` within the generated code is not matched
        match = re.search(pattern, plain_text, re.MULTILINE)
        if match:
            return match.group(1).strip()
        elif match_single_block:
            # Check for a single code block
            single_code_block_pattern = r"^```(?:\w+\n)?([\s\S]*?)```"
            single_code_block_match = re.findall(single_code_block_pattern, plain_text, re.MULTILINE)
            if len(single_code_block_match) == 1:
                return single_code_block_match[0].strip()
        return ''

    def write_config_yml(self, class_name, dest_folder, python_file='microservice.py'):
        config_content = f'''jtype: {class_name}
py_modules:
  - {python_file}
metas:
  name: {class_name}
'''
        with open(os.path.join(dest_folder, 'config.yml'), 'w', encoding='utf-8') as f:
            f.write(config_content)

    def files_to_string(self, file_name_to_content, restrict_keys=None):
        all_microservice_files_string = ''
        for file_name, tag in FILE_AND_TAG_PAIRS:
            if file_name in file_name_to_content and (not restrict_keys or file_name in restrict_keys):
                all_microservice_files_string += f'**{file_name}**\n```{tag}\n{file_name_to_content[file_name]}\n```\n\n'
        return all_microservice_files_string.strip()


    def generate_and_persist_file(
            self,
            section_title,
            template,
            destination_folder=None,
            file_name=None,
            system_definition_examples: List[str] = ['gpt', 'executor', 'docarray', 'client'],
            **template_kwargs
    ):
        """
        Generates a file using the GPT-3 API and persists it to the destination folder if specified.
        In case the content is not properly generated, it retries the generation.
        It returns the generated content.
        """
        print_colored('', f'\n\n############# {section_title} #############', 'blue')
        system_introduction_message = self._create_system_message(self.microservice_specification.task, self.microservice_specification.test, system_definition_examples)
        conversation = self.gpt_session.get_conversation(messages=[system_introduction_message])
        template_kwargs = {k: v for k, v in template_kwargs.items() if k in template.input_variables}
        content_raw = conversation.chat(
            template.format(
                file_name=file_name,
                **template_kwargs
            )
        )
        content = self.extract_content_from_result(content_raw, file_name, match_single_block=True)
        if content == '':
            content_raw = conversation.chat(f'You must add the content for {file_name}.')
            content = self.extract_content_from_result(
                content_raw, file_name, match_single_block=True
            )
        if destination_folder:
            persist_file(content, os.path.join(destination_folder, file_name))
        return content

    def generate_microservice(
            self,
            path,
            microservice_name,
            packages,
            num_approach,
    ):
        MICROSERVICE_FOLDER_v1 = get_microservice_path(path, microservice_name, packages, num_approach, 1)
        os.makedirs(MICROSERVICE_FOLDER_v1)

        microservice_content = self.generate_and_persist_file(
            'Microservice',
            template_generate_executor,
            MICROSERVICE_FOLDER_v1,
            microservice_name=microservice_name,
            microservice_description=self.microservice_specification.task,
            test_description=self.microservice_specification.test,
            packages=packages,
            file_name_purpose=EXECUTOR_FILE_NAME,
            tag_name=EXECUTOR_FILE_TAG,
            file_name=EXECUTOR_FILE_NAME,
        )

        test_microservice_content = self.generate_and_persist_file(
            'Test Microservice',
            template_generate_test,
            MICROSERVICE_FOLDER_v1,
            code_files_wrapped=self.files_to_string({'microservice.py': microservice_content}),
            microservice_name=microservice_name,
            microservice_description=self.microservice_specification.task,
            test_description=self.microservice_specification.test,
            file_name_purpose=TEST_EXECUTOR_FILE_NAME,
            tag_name=TEST_EXECUTOR_FILE_TAG,
            file_name=TEST_EXECUTOR_FILE_NAME,
        )

        requirements_content = self.generate_and_persist_file(
            'Requirements',
            template_generate_requirements,
            MICROSERVICE_FOLDER_v1,
            code_files_wrapped=self.files_to_string({
                'microservice.py': microservice_content,
                'test_microservice.py': test_microservice_content,
            }),
            file_name_purpose=REQUIREMENTS_FILE_NAME,
            file_name=REQUIREMENTS_FILE_NAME,
            tag_name=REQUIREMENTS_FILE_TAG,
        )

        self.generate_and_persist_file(
            'Dockerfile',
            template_generate_dockerfile,
            MICROSERVICE_FOLDER_v1,
            code_files_wrapped=self.files_to_string({
                'microservice.py': microservice_content,
                'test_microservice.py': test_microservice_content,
                'requirements.txt': requirements_content,
            }),
            file_name_purpose=DOCKER_FILE_NAME,
            file_name=DOCKER_FILE_NAME,
            tag_name=DOCKER_FILE_TAG,
        )

        self.write_config_yml(microservice_name, MICROSERVICE_FOLDER_v1)

        print('\nFirst version of the microservice generated. Start iterating on it to make the tests pass...')

    def generate_playground(self, microservice_name, microservice_path):
        print_colored('', '\n\n############# Playground #############', 'blue')

        file_name_to_content = get_all_microservice_files_with_content(microservice_path)
        conversation = self.gpt_session.get_conversation([])
        conversation.chat(
            template_generate_playground.format(
                code_files_wrapped=self.files_to_string(file_name_to_content, ['microservice.py', 'test_microservice.py']),
                microservice_name=microservice_name,
            )
        )
        playground_content_raw = conversation.chat(
            template_chain_of_thought.format(
                file_name_purpose='app.py/the playground',
                file_name='app.py',
                tag_name='python',
            )
        )
        playground_content = self.extract_content_from_result(playground_content_raw, 'app.py', match_single_block=True)
        if playground_content == '':
            content_raw = conversation.chat(f'You must add the app.py code. You most not output any other code')
            playground_content = self.extract_content_from_result(
                content_raw, 'app.py', match_single_block=True
            )

        gateway_path = os.path.join(microservice_path, 'gateway')
        shutil.copytree(os.path.join(os.path.dirname(__file__), 'static_files', 'gateway'), gateway_path)
        persist_file(playground_content, os.path.join(gateway_path, 'app.py'))

        # fill-in name of microservice
        gateway_name = f'Gateway{microservice_name}'
        custom_gateway_path = os.path.join(gateway_path, 'custom_gateway.py')
        with open(custom_gateway_path, 'r', encoding='utf-8') as f:
            custom_gateway_content = f.read()
        custom_gateway_content = custom_gateway_content.replace(
            'class CustomGateway(CompositeGateway):',
            f'class {gateway_name}(CompositeGateway):'
        )
        with open(custom_gateway_path, 'w', encoding='utf-8') as f:
            f.write(custom_gateway_content)

        # write config.yml
        self.write_config_yml(gateway_name, gateway_path, 'custom_gateway.py')

        # push the gateway
        print('Final step...')
        hubble_log = push_executor(gateway_path)
        if not is_executor_in_hub(gateway_name):
            raise Exception(f'{microservice_name} not in hub. Hubble logs: {hubble_log}')


    def debug_microservice(self, path, microservice_name, num_approach, packages):
        for i in range(1, MAX_DEBUGGING_ITERATIONS):
            print('Debugging iteration', i)
            print('Trying to debug the microservice. Might take a while...')
            previous_microservice_path = get_microservice_path(path, microservice_name, packages, num_approach, i)
            next_microservice_path = get_microservice_path(path, microservice_name, packages, num_approach, i + 1)
            log_hubble = push_executor(previous_microservice_path)
            error = process_error_message(log_hubble)
            if error:
                print('An error occurred during the build process. Feeding the error back to the assistent...')
                self.do_debug_iteration(error, next_microservice_path, previous_microservice_path)
                if i == MAX_DEBUGGING_ITERATIONS - 1:
                    raise self.MaxDebugTimeReachedException('Could not debug the microservice.')
            else:
                # at the moment, there can be cases where no error log is extracted but the executor is still not published
                # it leads to problems later on when someone tries a run or deployment
                if is_executor_in_hub(microservice_name):
                    print('Successfully build microservice.')
                    break
                else:
                    raise Exception(f'{microservice_name} not in hub. Hubble logs: {log_hubble}')


        return get_microservice_path(path, microservice_name, packages, num_approach, i)

    def do_debug_iteration(self, error, next_microservice_path, previous_microservice_path):
        os.makedirs(next_microservice_path)
        file_name_to_content = get_all_microservice_files_with_content(previous_microservice_path)

        summarized_error = self.summarize_error(error)
        is_dependency_issue = self.is_dependency_issue(error, file_name_to_content['Dockerfile'])
        if is_dependency_issue:
            all_files_string = self.files_to_string({
                key: val for key, val in file_name_to_content.items() if
                key in ['requirements.txt', 'Dockerfile']
            })
            user_query = template_solve_dependency_issue.format(
                summarized_error=summarized_error, all_files_string=all_files_string,
            )
        else:
            user_query = template_solve_code_issue.format(
                task_description=self.microservice_specification.task, test_description=self.microservice_specification.test,
                summarized_error=summarized_error, all_files_string=self.files_to_string(file_name_to_content),
            )
        conversation = self.gpt_session.get_conversation()
        returned_files_raw = conversation.chat(user_query)
        for file_name, tag in FILE_AND_TAG_PAIRS:
            updated_file = self.extract_content_from_result(returned_files_raw, file_name)
            if updated_file and (not is_dependency_issue or file_name in ['requirements.txt', 'Dockerfile']):
                file_name_to_content[file_name] = updated_file
                print(f'Updated {file_name}')
        for file_name, content in file_name_to_content.items():
            persist_file(content, os.path.join(next_microservice_path, file_name))

    class MaxDebugTimeReachedException(BaseException):
        pass

    def is_dependency_issue(self, error, docker_file: str):
        # a few heuristics to quickly jump ahead
        if any([error_message in error for error_message in ['AttributeError', 'NameError', 'AssertionError']]):
            return False

        print_colored('', 'Is it a dependency issue?', 'blue')
        conversation = self.gpt_session.get_conversation([])
        answer = conversation.chat(template_is_dependency_issue.format(error=error, docker_file=docker_file))
        return 'yes' in answer.lower()

    def generate_microservice_name(self, description):
        print_colored('', '\n\n############# What should be the name of the Microservice? #############', 'blue')
        conversation = self.gpt_session.get_conversation()
        name_raw = conversation.chat(template_generate_microservice_name.format(description=description))
        name = self.extract_content_from_result(name_raw, 'name.txt')
        return name

    def get_possible_packages(self):
        print_colored('', '\n\n############# What packages to use? #############', 'blue')
        packages_csv_string = self.generate_and_persist_file(
            'packages to use',
            template_generate_possible_packages,
            None,
            file_name='packages.csv',
            system_definition_examples=['gpt'],
            description=self.microservice_specification.task

        )
        packages_list = [[pkg.strip() for pkg in packages_string.split(',')] for packages_string in packages_csv_string.split('\n')]
        packages_list = packages_list[:NUM_IMPLEMENTATION_STRATEGIES]
        return packages_list

    def generate(self, microservice_path):
        self.refine_specification()
        generated_name = self.generate_microservice_name(self.microservice_specification.task)
        microservice_name = f'{generated_name}{random.randint(0, 10_000_000)}'
        packages_list = self.get_possible_packages()
        packages_list = [
            packages for packages in packages_list if len(set(packages).intersection(set(PROBLEMATIC_PACKAGES))) == 0
        ]
        packages_list = [
            [package for package in packages if package not in UNNECESSARY_PACKAGES] for packages in packages_list
        ]
        for num_approach, packages in enumerate(packages_list):
            try:
                self.generate_microservice(microservice_path, microservice_name, packages, num_approach)
                final_version_path = self.debug_microservice(
                    microservice_path, microservice_name, num_approach, packages
                )
                self.generate_playground(microservice_name, final_version_path)
            except self.MaxDebugTimeReachedException:
                print('Could not debug the Microservice with the approach:', packages)
                if num_approach == len(packages_list) - 1:
                    print_colored('',
                                  f'Could not debug the Microservice with any of the approaches: {packages} giving up.',
                                  'red')
                continue
            print(f'''
You can now run or deploy your microservice:
gptdeploy run --path {microservice_path}
gptdeploy deploy --path {microservice_path}
'''
                  )
            break

    def summarize_error(self, error):
        conversation = self.gpt_session.get_conversation([])
        error_summary = conversation.chat(template_summarize_error.format(error=error))
        return error_summary

    def refine_specification(self):
        pm = get_random_employee('pm')
        print(f'{pm.emoji}👋 Hi, I\'m {pm.name}, a PM at Jina AI. Gathering the requirements for our engineers.')
        self.refine_task(pm)
        self.refine_test(pm)
        print(f'''
{pm.emoji} 👍 Great, I will handover the following requirements to our engineers:
Description of the microservice:
{self.microservice_specification.task}
Test scenario:
{self.microservice_specification.test}
''')

    def refine_task(self, pm):

        task_description = self.microservice_specification.task
        if not task_description:
            task_description = self.get_user_input(pm, 'What should your microservice do?')
        messages = [
            SystemMessage(content=system_task_introduction + system_task_iteration),
        ]

        while True:
            conversation = self.gpt_session.get_conversation(messages, print_stream=os.environ['VERBOSE'].lower() == 'true', print_costs=False)
            print('thinking...')
            agent_response_raw = conversation.chat(task_description, role='user')

            question = self.extract_content_from_result(agent_response_raw, 'prompt.txt')
            task_final = self.extract_content_from_result(agent_response_raw, 'task-final.txt')
            if task_final:
                self.microservice_specification.task = task_final
                break
            if question:
                task_description = self.get_user_input(pm, question)
                messages.extend([HumanMessage(content=task_description)])
            else:
                task_description = self.get_user_input(pm, agent_response_raw + '\n: ')

    def refine_test(self, pm):
        messages = [
            SystemMessage(content=system_task_introduction + system_test_iteration),
        ]
        user_input = self.microservice_specification.task
        while True:
            conversation = self.gpt_session.get_conversation(messages, print_stream=os.environ['VERBOSE'].lower() == 'true', print_costs=False)
            agent_response_raw = conversation.chat(f'''**client-response.txt**
```
{user_input}
```
''', role='user')
            question = self.extract_content_from_result(agent_response_raw, 'prompt.txt')
            test_final = self.extract_content_from_result(agent_response_raw, 'test-final.txt')
            if test_final:
                self.microservice_specification.test = test_final
                break
            if question:
                user_input = self.get_user_input(pm, question)
                messages.extend([HumanMessage(content=user_input)])
            else:
                user_input = self.get_user_input(pm, agent_response_raw + '\n: ')


    @staticmethod
    def _create_system_message(task_description, test_description, system_definition_examples: List[str] = []) -> SystemMessage:
        system_message = PromptTemplate.from_template(template_system_message_base).format(
            task_description=task_description,
            test_description=test_description,
        )
        if 'gpt' in system_definition_examples:
            system_message += f'\n{gpt_example}'
        if 'executor' in system_definition_examples:
            system_message += f'\n{executor_example}'
        if 'docarray' in system_definition_examples:
            system_message += f'\n{docarray_example}'
        if 'client' in system_definition_examples:
            system_message += f'\n{client_example}'
        return SystemMessage(content=system_message)

    @staticmethod
    def get_user_input(employee, prompt_to_user):
        val = input(f'{employee.emoji}❓ {prompt_to_user}\nyou: ')
        while not val:
            val = input('you: ')
        return val
