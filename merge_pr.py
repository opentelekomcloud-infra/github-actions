import sys
import os
from github import Github
from github.GithubException import GithubException

def main():
    token = sys.argv[1]
    base_branch = sys.argv[2]
    pr_number = int(sys.argv[3])
    
    g = Github(token)
    repo = g.get_repo(os.environ['GITHUB_REPOSITORY'])
    
    try:
        pr = repo.get_pull(pr_number)
        
        if pr.is_merged():
            repo.merge(base_branch, pr.head.sha, f"Merge PR #{pr_number}")
            print(f"Successfully merged PR #{pr_number} into {base_branch}")
        else:
            print(f"PR #{pr_number} is not merged yet")
            sys.exit(1)
    except GithubException as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
