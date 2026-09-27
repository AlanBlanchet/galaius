"""Export the public account contracts as deterministic JSON Schema."""

import json
from typing import Union

from pydantic import TypeAdapter

from interact_core import (
    Account,
    AccountUpdate,
    PortableToolSettings, PortableToolSettingsUpdate, PortableToolSettingsValues,
    Bootstrap,
    CompanyProfile,
    CompanyProfileUpdate,
    CompanyLogoUpload,
    CompanyLookupResult,
    ReleaseInfo,
    LoginRequest,
    PasswordResetRequest,
    PlatformError,
    RecoveryRequest,
    SignupRequest,
    TokenRequest,
    Workspace,
    WorkspaceCreate,
    WorkspaceDeleteRequest,
    WorkspaceInvitation,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceMembership,
    CompanyAccess,
    WorkspaceUpdate,
)


def main() -> None:
    contract = Union[
        PortableToolSettings, PortableToolSettingsUpdate, PortableToolSettingsValues,
        CompanyProfile, CompanyProfileUpdate, CompanyLogoUpload, CompanyLookupResult,
        ReleaseInfo,
        Account, AccountUpdate, Bootstrap, SignupRequest, LoginRequest, TokenRequest,
        RecoveryRequest, PasswordResetRequest, PlatformError, Workspace, WorkspaceCreate, WorkspaceDeleteRequest, WorkspaceInvitation,
        WorkspaceInvite, WorkspaceMember, WorkspaceMembership, CompanyAccess, WorkspaceUpdate,
    ]
    print(json.dumps(TypeAdapter(contract).json_schema(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
